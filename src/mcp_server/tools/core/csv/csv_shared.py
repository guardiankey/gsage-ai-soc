"""csv_shared — utilities shared across all CSV tools.

Provides reusable helpers extracted to avoid code duplication between
``csv_query``, ``csv_soc``, and ``csv_edit``.

SQL sandbox
-----------
* :func:`validate_sql_safe`   — validates a full SELECT / WITH query.
* :func:`validate_where_sql`  — validates a WHERE-clause fragment.

DataFrame helpers
-----------------
* :func:`df_to_csv_bytes`         — serialize a Polars frame to CSV bytes.
* :func:`compute_edited_filename` — derive the output filename for edits
  (supports ``force_new`` and ``existing_filenames`` for suffix increments).

Filter helpers
--------------
* :func:`build_filter_predicate` — JSON filter spec → ``polars.Expr``.
* :func:`apply_where_sql`        — DuckDB-backed WHERE filter on a frame.
* :func:`get_where_sql_mask`     — boolean mask from a WHERE clause.

Value source helpers
--------------------
* :func:`resolve_value_source`   — ``{type, ...}`` spec → ``polars.Expr``.

Sort detection
--------------
* :func:`detect_sort_dtype` — heuristic dtype for sorting
  (``"numeric"`` / ``"date"`` / ``"alphanum"``).
"""

from __future__ import annotations

import ast
import hashlib
import logging
import re
from typing import Any, Optional

import polars as pl

logger = logging.getLogger(__name__)

# ── SQL Sandbox ─────────────────────────────────────────────────────────────
# Shared deny-lists used by csv_query, csv_soc (where_sql mode), and csv_edit.

_DENY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"\b{kw}\b", re.IGNORECASE)
    for kw in (
        "ATTACH",
        "DETACH",
        "INSTALL",
        "LOAD",
        "COPY",
        "EXPORT",
        "IMPORT",
        "PRAGMA",
        "SET",
        "RESET",
        "CREATE",
        "DROP",
        "INSERT",
        "UPDATE",
        "DELETE",
        "ALTER",
        "TRUNCATE",
        "CALL",
        "GRANT",
        "REVOKE",
        "VACUUM",
        "CHECKPOINT",
    )
)

_DENY_FUNCTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"\b{kw}\s*\(", re.IGNORECASE)
    for kw in (
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "read_json",
        "read_json_auto",
        "read_blob",
        "read_text",
        "parquet_scan",
        "json_scan",
        "iceberg_scan",
        "delta_scan",
        "glob",
        "list_dir",
        "httpfs",
    )
)

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def _strip_comments(sql: str) -> str:
    return _COMMENT_RE.sub(" ", sql)


def validate_sql_safe(sql: str) -> Optional[str]:
    """Return ``None`` if a full SELECT / WITH query looks safe; else an error string.

    Enforces the same rules as ``csv_query``:
    - Must be a single statement starting with SELECT or WITH.
    - Must not contain deny-listed DDL/DML keywords or filesystem functions.
    """
    stripped_for_check = _strip_comments(sql).strip().rstrip(";").strip()
    if not stripped_for_check:
        return "Empty SQL."

    body = _strip_comments(sql).strip()
    if body.rstrip(";").count(";") > 0:
        return "Multiple SQL statements are not allowed."

    head = stripped_for_check.lstrip("(").lstrip()
    head_word = head.split(None, 1)[0].upper() if head else ""
    if head_word not in {"SELECT", "WITH"}:
        return "Only SELECT or WITH ... SELECT queries are allowed."

    for pat in _DENY_PATTERNS:
        if pat.search(stripped_for_check):
            return f"Disallowed keyword: {pat.pattern}"
    for pat in _DENY_FUNCTION_PATTERNS:
        if pat.search(stripped_for_check):
            return f"Disallowed function: {pat.pattern}"

    return None


def validate_where_sql(where_sql: str) -> Optional[str]:
    """Return ``None`` if a WHERE-clause fragment looks safe; else an error string.

    Does *not* require the input to begin with SELECT — it validates only
    the expression that appears after the WHERE keyword.  Checks for:
    - Multiple statements (semicolons).
    - Deny-listed keywords and filesystem functions.
    """
    stripped = _strip_comments(where_sql).strip()
    if not stripped:
        return "Empty WHERE clause."
    if ";" in stripped:
        return "Semicolons are not allowed in WHERE clauses."

    for pat in _DENY_PATTERNS:
        if pat.search(stripped):
            return f"Disallowed keyword: {pat.pattern}"
    for pat in _DENY_FUNCTION_PATTERNS:
        if pat.search(stripped):
            return f"Disallowed function: {pat.pattern}"

    return None


# ── DuckDB internal helper ──────────────────────────────────────────────────


def _run_duckdb_sql(df: pl.DataFrame, sql: str) -> pl.DataFrame:
    """Execute *sql* against *df* registered as view ``csv`` and return a Polars frame.

    Synchronous — call via ``asyncio.to_thread`` from an async context.
    """
    import duckdb  # type: ignore[import-untyped]

    con = duckdb.connect(":memory:")
    try:
        con.execute("SET disabled_filesystems='LocalFileSystem'")
        con.execute("SET memory_limit='256MB'")
        con.execute("SET threads TO 2")
        con.register("csv", df.to_arrow())
        return con.execute(sql).pl()
    finally:
        try:
            con.close()
        except Exception:
            pass


def apply_where_sql(df: pl.DataFrame, where_sql: str) -> pl.DataFrame:
    """Filter *df* using a raw DuckDB WHERE-clause fragment.

    Call :func:`validate_where_sql` before this function.
    """
    return _run_duckdb_sql(df, f"SELECT * FROM csv WHERE ({where_sql})")


def get_where_sql_mask(df: pl.DataFrame, where_sql: str) -> pl.Series:
    """Return a boolean Series indicating rows that match *where_sql*.

    Call :func:`validate_where_sql` before this function.
    NULL-safe: WHERE conditions that evaluate to NULL are treated as non-matching.
    """
    mask_df = _run_duckdb_sql(
        df,
        f"SELECT CASE WHEN ({where_sql}) THEN true ELSE false END AS __mask FROM csv",
    )
    return mask_df["__mask"]


# ── DataFrame utilities ─────────────────────────────────────────────────────


def df_to_csv_bytes(df: pl.DataFrame) -> bytes:
    """Serialize a Polars frame to UTF-8 encoded CSV bytes."""
    return df.write_csv().encode("utf-8")


# Matches filenames that already carry an _edited<N>.csv suffix.
# Group 1 = base name (before _edited), group 2 = optional numeric suffix.
_EDITED_SUFFIX_RE: re.Pattern[str] = re.compile(
    r"^(.+?)_edited(\d*)(\.csv)$", re.IGNORECASE
)


def compute_edited_filename(
    filename: str,
    *,
    force_new: bool = False,
    existing_filenames: Optional[set[str]] = None,
) -> tuple[str, bool]:
    """Derive the output filename and whether to edit in-place.

    Returns ``(output_filename, is_inplace)``.

    In-place overwrite (``is_inplace=True``) happens when *filename* already
    carries the ``_edited<N>.csv`` suffix **and** *force_new* is ``False``.

    New-file creation (``is_inplace=False``) happens when *force_new* is
    ``True`` or *filename* does not carry an ``_edited*`` suffix.  The
    function picks the first available name from
    ``<base>_edited.csv``, ``<base>_edited2.csv``, ``<base>_edited3.csv``, …
    that is not present in *existing_filenames* (case-insensitive) and is
    not identical to *filename* itself.

    Parameters
    ----------
    filename:
        Source filename.
    force_new:
        When ``True``, always create a new file even if *filename* is
        already ``_edited*``.  Default ``False``.
    existing_filenames:
        Filenames already in use (e.g. from a DB query).  Used to select
        the next free increment.  ``None`` is treated as an empty set.
    """
    existing_lower = {f.lower() for f in (existing_filenames or set())}
    source_lower = filename.lower()
    m = _EDITED_SUFFIX_RE.match(filename)

    if m and not force_new:
        # Source already carries the _edited* suffix → overwrite in-place.
        return filename, True

    # Compute base (strip any _edited* suffix and .csv extension).
    if m:
        base = m.group(1)
    elif source_lower.endswith(".csv"):
        base = filename[:-4]
    else:
        base = filename

    # Find the first available _edited<N>.csv name.
    candidate = f"{base}_edited.csv"
    if candidate.lower() != source_lower and candidate.lower() not in existing_lower:
        return candidate, False
    i = 2
    while True:
        candidate = f"{base}_edited{i}.csv"
        if candidate.lower() != source_lower and candidate.lower() not in existing_lower:
            return candidate, False
        i += 1


async def _fetch_edited_filenames(
    org_id: Any,
    user_id: Any,
    original_filename: str,
    session: Any,
) -> set[str]:
    """Return non-purged filenames matching '<base>_edited*.csv' for the user.

    The *base* is extracted from *original_filename* by stripping any
    ``_edited<N>`` suffix and the ``.csv`` extension.  Only files owned by
    *user_id* within *org_id* are returned.

    Acquires a PostgreSQL advisory transaction lock on ``(org_id, base)``
    before querying, preventing concurrent requests from allocating the same
    suffix.  The lock is released automatically when the surrounding
    transaction commits.
    """
    from src.shared.models.generated_file import GSageFile  # noqa: PLC0415
    from sqlalchemy import select, text  # noqa: PLC0415

    m = _EDITED_SUFFIX_RE.match(original_filename)
    if m:
        base = m.group(1)
    elif original_filename.lower().endswith(".csv"):
        base = original_filename[:-4]
    else:
        base = original_filename

    # Acquire a PostgreSQL advisory transaction lock keyed on (org_id, base)
    # to prevent race conditions when concurrent requests allocate the next
    # available suffix simultaneously.  The lock is held until the surrounding
    # session commits (covering the full query → insert window).
    lock_bytes = hashlib.md5(f"{org_id}:{base}".encode()).digest()
    lock_int = int.from_bytes(lock_bytes[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=lock_int))

    stmt = (
        select(GSageFile.filename)
        .where(
            GSageFile.org_id == org_id,
            GSageFile.user_id == user_id,
            GSageFile.filename.ilike(f"{base}_edited%.csv"),
            GSageFile.purged_at.is_(None),
        )
    )
    result = await session.execute(stmt)
    return {row[0] for row in result.all()}


# ── Structured filter ───────────────────────────────────────────────────────

_VALID_OPS = frozenset(
    {
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "contains",
        "starts_with",
        "ends_with",
        "regex",
        "in",
        "not_in",
        "is_null",
        "is_not_null",
    }
)


def _build_single_filter(item: dict, columns: list[str]) -> pl.Expr:
    """Build a Polars boolean Expr for a single filter condition."""
    col_name = item.get("column")
    op = item.get("op")
    value = item.get("value")
    ignore_case = bool(item.get("ignore_case", False))

    if not isinstance(col_name, str) or col_name not in columns:
        raise ValueError(f"Filter column {col_name!r} not found in DataFrame.")
    if op not in _VALID_OPS:
        raise ValueError(
            f"Unknown filter op {op!r}. Valid ops: {sorted(_VALID_OPS)}"
        )

    col_expr = pl.col(col_name)

    if op == "is_null":
        return col_expr.is_null()
    if op == "is_not_null":
        return col_expr.is_not_null()

    # For text ops with ignore_case, compare lowercased strings.
    text_ops = {"eq", "ne", "contains", "starts_with", "ends_with", "regex"}
    if ignore_case and isinstance(value, str) and op in text_ops:
        col_str = col_expr.cast(pl.Utf8, strict=False).str.to_lowercase()
        cmp: object = value.lower()
    else:
        col_str = col_expr.cast(pl.Utf8, strict=False)
        cmp = value

    if op == "eq":
        return col_str == pl.lit(cmp) if ignore_case else col_expr == pl.lit(value)
    if op == "ne":
        return col_str != pl.lit(cmp) if ignore_case else col_expr != pl.lit(value)
    if op == "gt":
        return col_expr > pl.lit(value)
    if op == "gte":
        return col_expr >= pl.lit(value)
    if op == "lt":
        return col_expr < pl.lit(value)
    if op == "lte":
        return col_expr <= pl.lit(value)
    if op == "contains":
        return col_str.str.contains(str(cmp), literal=True)
    if op == "starts_with":
        return col_str.str.starts_with(str(cmp))
    if op == "ends_with":
        return col_str.str.ends_with(str(cmp))
    if op == "regex":
        pattern = f"(?i){value}" if ignore_case else str(value)
        return col_expr.cast(pl.Utf8, strict=False).str.contains(pattern, literal=False)
    if op == "in":
        if not isinstance(value, list):
            raise ValueError("'in' operator requires a list value.")
        if ignore_case:
            return col_expr.cast(pl.Utf8, strict=False).str.to_lowercase().is_in(
                [str(v).lower() for v in value]
            )
        return col_expr.is_in(value)
    if op == "not_in":
        if not isinstance(value, list):
            raise ValueError("'not_in' operator requires a list value.")
        if ignore_case:
            return ~col_expr.cast(pl.Utf8, strict=False).str.to_lowercase().is_in(
                [str(v).lower() for v in value]
            )
        return ~col_expr.is_in(value)

    raise ValueError(f"Unhandled op: {op!r}")  # unreachable


def build_filter_predicate(filter_spec: dict, columns: list[str]) -> pl.Expr:
    """Convert a structured filter spec into a Polars boolean ``Expr``.

    The spec must have exactly one of ``"all"`` (AND) or ``"any"`` (OR) keys,
    each mapping to a non-empty list of condition dicts.

    Example::

        {"all": [
            {"column": "ip", "op": "contains", "value": "10.", "ignore_case": false},
            {"column": "port", "op": "in", "value": [80, 443]},
        ]}

    Each condition dict fields:

    - ``column``      — column name (required).
    - ``op``          — one of: eq, ne, gt, gte, lt, lte, contains,
                         starts_with, ends_with, regex, in, not_in,
                         is_null, is_not_null.
    - ``value``       — comparison value (not required for is_null / is_not_null).
    - ``ignore_case`` — bool; applies to text ops (default false).
    """
    if "all" in filter_spec and "any" in filter_spec:
        raise ValueError("Filter spec may have 'all' or 'any', not both.")
    if "all" not in filter_spec and "any" not in filter_spec:
        raise ValueError("Filter spec must have an 'all' or 'any' key.")

    key = "all" if "all" in filter_spec else "any"
    items = filter_spec[key]
    if not isinstance(items, list) or not items:
        raise ValueError(f"'{key}' must be a non-empty list of filter conditions.")

    exprs = [_build_single_filter(item, columns) for item in items]
    combined = exprs[0]
    for e in exprs[1:]:
        combined = combined & e if key == "all" else combined | e
    return combined


# ── Value source resolution ─────────────────────────────────────────────────


def _parse_arith_to_polars(expr_str: str, df_columns: list[str]) -> pl.Expr:
    """Parse a safe arithmetic expression string into a Polars ``Expr``.

    Only allows: column names, numeric / string literals, binary arithmetic
    operators (``+ - * / // % **``), and unary negation.  Function calls,
    attribute access, and any other AST node types are rejected.
    """
    try:
        tree = ast.parse(expr_str.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"Invalid arithmetic expression: {exc}") from exc
    return _visit_arith_node(tree.body, df_columns)


def _visit_arith_node(node: ast.expr, columns: list[str]) -> pl.Expr:  # type: ignore[name-defined]
    if isinstance(node, ast.Name):
        if node.id not in columns:
            raise ValueError(
                f"Column {node.id!r} not found in DataFrame "
                f"(arithmetic expression)."
            )
        return pl.col(node.id)

    if isinstance(node, ast.Constant):
        return pl.lit(node.value)

    if isinstance(node, ast.BinOp):
        left = _visit_arith_node(node.left, columns)
        right = _visit_arith_node(node.right, columns)
        op = node.op
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.Div):
            return left / right
        if isinstance(op, ast.FloorDiv):
            return left // right
        if isinstance(op, ast.Mod):
            return left % right
        if isinstance(op, ast.Pow):
            return left**right
        raise ValueError(f"Unsupported binary operator: {type(op).__name__}")

    if isinstance(node, ast.UnaryOp):
        operand = _visit_arith_node(node.operand, columns)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")

    raise ValueError(
        f"Unsupported expression node '{type(node).__name__}'. "
        "Only column references, numeric/string literals, and arithmetic "
        "operators are allowed."
    )


def resolve_value_source(spec: dict, df_columns: list[str]) -> pl.Expr:
    """Convert a value source spec into a Polars ``Expr``.

    Supported types:

    ``{"type": "literal", "value": <any>}``
        Constant value (string, number, bool, or null).

    ``{"type": "column", "column": "X"}``
        Copy from column X (must already exist).

    ``{"type": "concat", "parts": [...], "sep": ""}``
        Concatenate columns / literal strings.  Each part is either
        ``{"column": "X"}`` or ``{"literal": "text"}``.  ``sep`` is the
        separator inserted between parts (default: ``""``).

    ``{"type": "arith", "expr": "A + B * 2"}``
        Safe arithmetic expression.  Column names reference DataFrame
        columns; only ``+ - * / // % **`` and numeric literals allowed.
    """
    src_type = spec.get("type")

    if src_type == "literal":
        return pl.lit(spec.get("value"))

    if src_type == "column":
        col = spec.get("column")
        if not isinstance(col, str) or col not in df_columns:
            raise ValueError(f"Source column {col!r} not found.")
        return pl.col(col)

    if src_type == "concat":
        parts = spec.get("parts", [])
        sep = str(spec.get("sep", ""))
        if not isinstance(parts, list) or not parts:
            raise ValueError("'concat' requires at least one part.")
        exprs: list[pl.Expr] = []
        for p in parts:
            if "column" in p:
                c = p["column"]
                if c not in df_columns:
                    raise ValueError(f"Concat source column {c!r} not found.")
                exprs.append(pl.col(c).cast(pl.Utf8, strict=False))
            elif "literal" in p:
                exprs.append(pl.lit(str(p["literal"])))
            else:
                raise ValueError(
                    f"Unknown concat part key in {p!r}. Use 'column' or 'literal'."
                )
        return pl.concat_str(exprs, separator=sep, ignore_nulls=True)

    if src_type == "arith":
        expr_str = spec.get("expr", "")
        if not isinstance(expr_str, str) or not expr_str.strip():
            raise ValueError("'arith' requires a non-empty 'expr' string.")
        return _parse_arith_to_polars(expr_str, df_columns)

    raise ValueError(
        f"Unknown value source type {src_type!r}. "
        "Must be one of: 'literal', 'column', 'concat', 'arith'."
    )


# ── Sort type detection ─────────────────────────────────────────────────────

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_DMY_DATE_RE = re.compile(r"^\d{2}[/\-]\d{2}[/\-]\d{4}")
_YMD_COMPACT_RE = re.compile(r"^\d{8}$")
_DATE_PATTERNS = (_ISO_DATE_RE, _DMY_DATE_RE, _YMD_COMPACT_RE)


def detect_sort_dtype(series: pl.Series) -> str:
    """Detect the best sort key type for *series*.

    Returns one of ``"numeric"``, ``"date"``, or ``"alphanum"``.

    Heuristic:

    1. If all non-null values can be cast to ``Float64`` → ``"numeric"``.
    2. If ≥ 50 % of a sample of non-null string values look like a common
       date pattern (ISO 8601, DD/MM/YYYY, YYYYMMDD) → ``"date"``.
    3. Otherwise → ``"alphanum"``.
    """
    non_null = series.drop_nulls()
    if non_null.is_empty():
        return "alphanum"

    try:
        non_null.cast(pl.Float64, strict=True)
        return "numeric"
    except Exception:
        pass

    sample = non_null.cast(pl.Utf8, strict=False).head(30).to_list()
    if not sample:
        return "alphanum"

    matches = sum(
        1 for v in sample if v and any(p.match(v) for p in _DATE_PATTERNS)
    )
    if matches >= max(1, len(sample) // 2):
        return "date"

    return "alphanum"
