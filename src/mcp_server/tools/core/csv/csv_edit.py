"""csv_edit — in-place and copy editing of stored CSV files.

Provides a single tool ``csv_edit`` with eleven actions:

* ``add_column``     — append a new column computed from a value source.
* ``set_column``     — update values in an existing column, optionally
                        filtered to a subset of rows.
* ``delete_rows``    — remove rows that match a filter condition.
* ``delete_columns`` — drop one or more columns.
* ``rename_columns`` — rename columns via an old→new mapping.
* ``sort``           — sort rows by one or more columns with type
                        auto-detection (numeric / date / alphanum).
* ``deduplicate``    — drop duplicate rows (optionally subset of columns).
* ``filter_to_new``  — materialise only rows matching a filter into a new file.
* ``cast_column``    — change a column's data type.
* ``split_column``   — split one column into N columns by separator or regex.
* ``fill_nulls``     — fill null values in a column with a literal or another column.

Output files
------------
* If the source ``filename`` does **not** end with ``_edited.csv`` → a new
  file named ``<base>_edited.csv`` is created (new ``file_id``).
* If the source ``filename`` already ends with ``_edited.csv`` → the same
  file is **overwritten in-place** (same ``file_id``, ``size_bytes`` updated).

Filters
-------
Each write action (``set_column``, ``delete_rows``, ``filter_to_new``) accepts
an optional filter to select which rows are affected.  Two mutually exclusive
modes:

``filter`` (structured JSON)
    ``{"all": [...]}`` for AND, ``{"any": [...]}`` for OR.  Each condition:
    ``{"column", "op", "value", "ignore_case"}``.  Supported ops: eq, ne, gt,
    gte, lt, lte, contains, starts_with, ends_with, regex, in, not_in,
    is_null, is_not_null.

``where_sql`` (DuckDB WHERE clause)
    A raw DuckDB WHERE-clause fragment validated against the same deny-list as
    ``csv_query``.  Use for complex multi-column or subquery conditions.

Value sources (``add_column`` / ``set_column`` / ``fill_nulls``)
-----------------------------------------------------------------
``{"type": "literal", "value": <any>}``
    Constant value.

``{"type": "column", "column": "X"}``
    Copy from existing column X.

``{"type": "concat", "parts": [...], "sep": ""}``
    Concatenate columns and/or string literals.

``{"type": "arith", "expr": "A + B * 2"}``
    Safe arithmetic expression — column names reference DataFrame columns;
    only ``+ - * / // % **`` and numeric/string literals allowed.

Permission: ``core:csv_edit``
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, ClassVar, Optional

import polars as pl

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.core.csv.csv_loader import load_csv, invalidate_cache
from src.mcp_server.tools.core.csv.csv_shared import (
    _fetch_edited_filenames,
    apply_where_sql,
    build_filter_predicate,
    compute_edited_filename,
    detect_sort_dtype,
    df_to_csv_bytes,
    get_where_sql_mask,
    resolve_value_source,
    validate_where_sql,
)
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ── Limits ─────────────────────────────────────────────────────────────────
_MAX_NEW_COLUMNS: int = 50
_MAX_SPLIT_PARTS: int = 20

# ── Action helpers (synchronous — run in worker thread) ─────────────────────


def _action_add_column(
    df: pl.DataFrame,
    *,
    column: str,
    value: dict,
    overwrite: bool,
) -> pl.DataFrame:
    if column in df.columns and not overwrite:
        raise ValueError(
            f"Column {column!r} already exists. "
            "Set overwrite=true to replace it, or use action='set_column'."
        )
    expr = resolve_value_source(value, df.columns).alias(column)
    return df.with_columns(expr)


def _action_set_column(
    df: pl.DataFrame,
    *,
    column: str,
    value: dict,
    filter_spec: Optional[dict],
    where_sql: Optional[str],
) -> pl.DataFrame:
    if column not in df.columns:
        raise ValueError(
            f"Column {column!r} not found. Use action='add_column' to create it."
        )
    new_val_expr = resolve_value_source(value, df.columns)

    if filter_spec is not None:
        predicate = build_filter_predicate(filter_spec, df.columns)
        return df.with_columns(
            pl.when(predicate)
            .then(new_val_expr)
            .otherwise(pl.col(column))
            .alias(column)
        )

    if where_sql is not None:
        mask = get_where_sql_mask(df, where_sql)
        df_with_mask = df.with_columns(mask.alias("__edit_mask__"))
        result = df_with_mask.with_columns(
            pl.when(pl.col("__edit_mask__"))
            .then(new_val_expr)
            .otherwise(pl.col(column))
            .alias(column)
        )
        return result.drop("__edit_mask__")

    # No filter — update all rows.
    return df.with_columns(new_val_expr.alias(column))


def _action_delete_rows(
    df: pl.DataFrame,
    *,
    filter_spec: Optional[dict],
    where_sql: Optional[str],
) -> pl.DataFrame:
    if filter_spec is not None:
        predicate = build_filter_predicate(filter_spec, df.columns)
        return df.filter(~predicate)

    if where_sql is not None:
        return apply_where_sql(df, f"NOT ({where_sql})")

    raise ValueError(
        "delete_rows requires either 'filter' or 'where_sql' to specify which rows to remove."
    )


def _action_delete_columns(df: pl.DataFrame, *, columns: list[str]) -> pl.DataFrame:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"Column(s) not found: {missing}")
    return df.drop(columns)


def _action_rename_columns(df: pl.DataFrame, *, column_map: dict[str, str]) -> pl.DataFrame:
    missing = [old for old in column_map if old not in df.columns]
    if missing:
        raise ValueError(f"Column(s) to rename not found: {missing}")
    # Check for conflicts: new names must not collide with columns that are NOT being renamed.
    renamed_set = set(column_map.keys())
    remaining = [c for c in df.columns if c not in renamed_set]
    new_names = set(column_map.values())
    conflicts = new_names & set(remaining)
    if conflicts:
        raise ValueError(
            f"Rename would overwrite existing column(s): {sorted(conflicts)}. "
            "Drop or rename those columns first."
        )
    return df.rename(column_map)


def _action_sort(df: pl.DataFrame, *, by: list[dict]) -> pl.DataFrame:
    if not by:
        raise ValueError("'by' must be a non-empty list of sort specifications.")

    sort_exprs: list[pl.Expr] = []
    descending: list[bool] = []

    for spec in by:
        col_name = spec.get("column")
        if not isinstance(col_name, str) or col_name not in df.columns:
            raise ValueError(f"Sort column {col_name!r} not found.")
        sort_desc = bool(spec.get("desc", False))
        dtype_hint = spec.get("type", "auto")
        if dtype_hint not in {"auto", "numeric", "date", "alphanum"}:
            raise ValueError(
                f"Invalid sort type {dtype_hint!r}. Must be 'auto', 'numeric', 'date', or 'alphanum'."
            )

        dtype = detect_sort_dtype(df[col_name]) if dtype_hint == "auto" else dtype_hint

        if dtype == "numeric":
            expr = pl.col(col_name).cast(pl.Float64, strict=False)
        elif dtype == "date":
            # Try Polars auto-parse to Date; fall back to string if it fails.
            try:
                df[col_name].cast(pl.Date, strict=True)
                expr = pl.col(col_name).cast(pl.Date, strict=False)
            except Exception:
                expr = pl.col(col_name).cast(pl.Utf8, strict=False)
        else:
            expr = pl.col(col_name).cast(pl.Utf8, strict=False)

        sort_exprs.append(expr)
        descending.append(sort_desc)

    return df.sort(by=sort_exprs, descending=descending, nulls_last=True)


def _action_deduplicate(
    df: pl.DataFrame,
    *,
    subset: Optional[list[str]],
    keep: str,
) -> pl.DataFrame:
    if keep not in {"first", "last", "none"}:
        raise ValueError("'keep' must be 'first', 'last', or 'none'.")
    if subset is not None:
        missing = [c for c in subset if c not in df.columns]
        if missing:
            raise ValueError(f"Dedup subset column(s) not found: {missing}")
    # Polars uses "any" for keep="none"
    from typing import Literal  # noqa: PLC0415
    _keep_map: dict[str, Literal["first", "last", "any"]] = {
        "first": "first",
        "last": "last",
        "none": "any",
    }
    polars_keep = _keep_map[keep]
    return df.unique(subset=subset, keep=polars_keep, maintain_order=True)


def _action_filter_to_new(
    df: pl.DataFrame,
    *,
    filter_spec: Optional[dict],
    where_sql: Optional[str],
) -> pl.DataFrame:
    if filter_spec is not None:
        predicate = build_filter_predicate(filter_spec, df.columns)
        return df.filter(predicate)

    if where_sql is not None:
        return apply_where_sql(df, where_sql)

    raise ValueError(
        "filter_to_new requires either 'filter' or 'where_sql'."
    )


_CAST_TYPE_MAP: dict[str, Any] = {
    "integer": pl.Int64,
    "float": pl.Float64,
    "string": pl.Utf8,
    "boolean": pl.Boolean,
    "date": pl.Date,
    "datetime": pl.Datetime,
}


def _action_cast_column(
    df: pl.DataFrame,
    *,
    column: str,
    target_type: str,
    date_format: Optional[str],
    strict: bool,
) -> pl.DataFrame:
    if column not in df.columns:
        raise ValueError(f"Column {column!r} not found.")
    if target_type not in _CAST_TYPE_MAP:
        raise ValueError(
            f"Unknown target_type {target_type!r}. "
            f"Must be one of: {sorted(_CAST_TYPE_MAP)}"
        )

    polars_type = _CAST_TYPE_MAP[target_type]

    if target_type in ("date", "datetime") and date_format:
        if target_type == "date":
            expr = pl.col(column).cast(pl.Utf8).str.to_date(
                format=date_format, strict=strict
            )
        else:
            expr = pl.col(column).cast(pl.Utf8).str.to_datetime(
                format=date_format, strict=strict
            )
    else:
        expr = pl.col(column).cast(polars_type, strict=strict)

    try:
        return df.with_columns(expr.alias(column))
    except Exception as exc:
        raise ValueError(
            f"Failed to cast column {column!r} to {target_type!r}: {exc}. "
            "Try setting strict=false to fill unparseable values with null."
        ) from exc


def _action_split_column(
    df: pl.DataFrame,
    *,
    column: str,
    separator: Optional[str],
    pattern: Optional[str],
    new_columns: Optional[list[str]],
    n: int,
    drop_original: bool,
) -> pl.DataFrame:
    if column not in df.columns:
        raise ValueError(f"Column {column!r} not found.")
    if n < 2 or n > _MAX_SPLIT_PARTS:
        raise ValueError(f"'n' must be between 2 and {_MAX_SPLIT_PARTS}.")

    col_names = new_columns or [f"{column}_{i}" for i in range(n)]
    if len(col_names) != n:
        raise ValueError(
            f"Length of 'new_columns' ({len(col_names)}) must equal 'n' ({n})."
        )

    str_series = df[column].cast(pl.Utf8, strict=False)

    if pattern:
        compiled = re.compile(pattern)

        def _regex_split(val: Optional[str]) -> list[Optional[str]]:
            if val is None:
                return [None] * n
            parts = compiled.split(val, maxsplit=n - 1)
            parts = parts[:n]
            while len(parts) < n:
                parts.append(None)
            return parts

        raw_lists = str_series.map_elements(_regex_split, return_dtype=pl.List(pl.Utf8))
    else:
        sep = separator or ","
        # str.split_exact splits into exactly n parts (fields 0..n-1)
        struct_col = str_series.str.split_exact(sep, n=n - 1)
        raw_lists = struct_col.struct.unnest()

        # After unnesting we have a DataFrame; re-join to original, then extract
        # column by column (struct unnest approach).
        unnested_df = pl.DataFrame({
            col_names[i]: struct_col.struct.field(f"field_{i}")
            for i in range(n)
        })
        result = df
        for col_name, series in zip(col_names, unnested_df):
            result = result.with_columns(series.alias(col_name))
        if drop_original:
            result = result.drop(column)
        return result

    # Pattern-based split: raw_lists is a Series of List[Utf8]
    result = df
    for i, col_name in enumerate(col_names):
        result = result.with_columns(
            raw_lists.list.get(i).alias(col_name)
        )
    if drop_original:
        result = result.drop(column)
    return result


def _action_fill_nulls(
    df: pl.DataFrame,
    *,
    column: str,
    value: dict,
    filter_spec: Optional[dict],
    where_sql: Optional[str],
) -> pl.DataFrame:
    if column not in df.columns:
        raise ValueError(f"Column {column!r} not found.")
    fill_expr = resolve_value_source(value, df.columns)

    # Build the fill expression: only set where original IS NULL, then apply filter
    null_mask = pl.col(column).is_null()

    if filter_spec is not None:
        predicate = build_filter_predicate(filter_spec, df.columns)
        combined = null_mask & predicate
    elif where_sql is not None:
        where_mask = get_where_sql_mask(df, where_sql)
        df = df.with_columns(where_mask.alias("__where_mask__"))
        combined = null_mask & pl.col("__where_mask__")
        result = df.with_columns(
            pl.when(combined).then(fill_expr).otherwise(pl.col(column)).alias(column)
        ).drop("__where_mask__")
        return result
    else:
        combined = null_mask

    return df.with_columns(
        pl.when(combined).then(fill_expr).otherwise(pl.col(column)).alias(column)
    )


# ── Tool ────────────────────────────────────────────────────────────────────


class CsvEditTool(BaseTool):
    """Edit a stored CSV file: add / update / delete columns and rows,
    sort, deduplicate, cast types, split columns, and fill nulls.

    All write actions produce a new output file (or edit in-place when the
    source already has the ``_edited.csv`` suffix) and return its ``file_id``
    so the agent can chain further edits or pass it to ``csv_query`` /
    ``csv_describe``.

    Permission: ``core:csv_edit``
    """

    name: ClassVar[str] = "csv_edit"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Edit a stored CSV file: add/update/delete columns and rows, "
        "sort, deduplicate, cast types, split columns, and fill nulls. "
        "Returns a new file_id (or the same one when editing an _edited.csv file)."
    )
    category: ClassVar[str] = "data"
    permissions: ClassVar[list[str]] = ["core:csv_edit"]
    rate_limit_per_minute: ClassVar[int] = 200
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action", "file_id"],
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "add_column",
                    "set_column",
                    "delete_rows",
                    "delete_columns",
                    "rename_columns",
                    "sort",
                    "deduplicate",
                    "filter_to_new",
                    "cast_column",
                    "split_column",
                    "fill_nulls",
                ],
                "description": (
                    "Editing action to perform:\n"
                    "- add_column: append a new column.\n"
                    "- set_column: update values in an existing column (optionally filtered).\n"
                    "- delete_rows: remove rows matching a filter.\n"
                    "- delete_columns: drop one or more columns.\n"
                    "- rename_columns: rename columns via an old→new mapping.\n"
                    "- sort: sort rows by one or more columns.\n"
                    "- deduplicate: drop duplicate rows.\n"
                    "- filter_to_new: keep only rows matching a filter.\n"
                    "- cast_column: change a column's data type.\n"
                    "- split_column: split one column into N columns.\n"
                    "- fill_nulls: fill null values with a literal or another column."
                ),
            },
            "file_id": {
                "type": "string",
                "description": "UUID of the source CSV file (GSageFile.id).",
            },
            # ── add_column / set_column / fill_nulls ────────────────────
            "column": {
                "type": "string",
                "description": (
                    "Target column name.\n"
                    "- add_column: name of the new column to create.\n"
                    "- set_column / fill_nulls: name of the existing column to update.\n"
                    "- cast_column / split_column: column to operate on."
                ),
            },
            "value": {
                "type": "object",
                "description": (
                    "Value source spec for add_column, set_column, fill_nulls.\n"
                    "Supported types:\n"
                    '  {"type": "literal", "value": <any>}\n'
                    '  {"type": "column", "column": "X"}\n'
                    '  {"type": "concat", "parts": [{"column":"A"},{"literal":" - "},{"column":"B"}], "sep": ""}\n'
                    '  {"type": "arith", "expr": "A + B * 2"}'
                ),
            },
            "overwrite": {
                "type": "boolean",
                "description": (
                    "add_column only. When true, overwrite if the column already "
                    "exists. Default false."
                ),
            },
            # ── delete_columns ──────────────────────────────────────────
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "delete_columns: list of column names to drop.",
            },
            # ── rename_columns ──────────────────────────────────────────
            "column_map": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "rename_columns: mapping of {old_name: new_name}.",
            },
            # ── sort ─────────────────────────────────────────────────────
            "by": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["column"],
                    "properties": {
                        "column": {"type": "string"},
                        "desc": {
                            "type": "boolean",
                            "description": "Sort descending. Default false.",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["auto", "numeric", "date", "alphanum"],
                            "description": (
                                "Sort key type. 'auto' detects automatically. Default 'auto'."
                            ),
                        },
                    },
                },
                "minItems": 1,
                "description": "sort: list of sort specs.",
            },
            # ── deduplicate ──────────────────────────────────────────────
            "subset": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "deduplicate: columns to consider for duplicates. "
                    "Omit to use all columns."
                ),
            },
            "keep": {
                "type": "string",
                "enum": ["first", "last", "none"],
                "description": (
                    "deduplicate: which duplicate to keep. "
                    "'none' removes all rows that have any duplicate. Default 'first'."
                ),
            },
            # ── cast_column ──────────────────────────────────────────────
            "target_type": {
                "type": "string",
                "enum": ["integer", "float", "string", "boolean", "date", "datetime"],
                "description": "cast_column: target data type.",
            },
            "date_format": {
                "type": "string",
                "description": (
                    "cast_column: strftime format string for date/datetime parsing "
                    "(e.g. '%Y-%m-%d', '%d/%m/%Y %H:%M:%S'). "
                    "Omit to let Polars auto-detect."
                ),
            },
            "strict": {
                "type": "boolean",
                "description": (
                    "cast_column: when false (default), unparseable values become null "
                    "instead of raising an error."
                ),
            },
            # ── split_column ─────────────────────────────────────────────
            "separator": {
                "type": "string",
                "description": "split_column: literal separator string (e.g. ':').",
            },
            "pattern": {
                "type": "string",
                "description": "split_column: regex pattern to split on (overrides separator).",
            },
            "new_columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "split_column: names for the resulting columns. "
                    "Length must equal 'n'. Auto-named if omitted."
                ),
            },
            "n": {
                "type": "integer",
                "minimum": 2,
                "maximum": _MAX_SPLIT_PARTS,
                "description": "split_column: number of parts to split into.",
            },
            "drop_original": {
                "type": "boolean",
                "description": "split_column: remove the source column. Default false.",
            },
            # ── Filters (shared by set_column, delete_rows, filter_to_new, fill_nulls) ──
            "filter": {
                "type": "object",
                "description": (
                    "Structured row filter. Mutually exclusive with 'where_sql'.\n"
                    "Use exactly one of 'all' (AND) or 'any' (OR):\n"
                    '  {"all": [{"column":"A","op":"contains","value":"foo","ignore_case":true}]}\n'
                    "Supported ops: eq, ne, gt, gte, lt, lte, contains, starts_with, "
                    "ends_with, regex, in, not_in, is_null, is_not_null."
                ),
            },
            "where_sql": {
                "type": "string",
                "description": (
                    "DuckDB WHERE-clause fragment (advanced). "
                    "Mutually exclusive with 'filter'. "
                    "Example: \"status = 'active' AND score > 80\". "
                    "The CSV is exposed as a view named 'csv'. "
                    "DDL / DML / filesystem functions are rejected."
                ),
            },
            # ── CSV parsing overrides ────────────────────────────────────
            "delimiter": {
                "type": "string",
                "description": (
                    "Override delimiter detection. Allowed: ',', ';', '\\t', '|'. "
                    "Omit for auto-detect."
                ),
            },
            "encoding": {
                "type": "string",
                "description": "Override encoding detection (e.g. 'utf-8', 'latin-1').",
            },
            "create_new": {
                "type": "boolean",
                "description": (
                    "Controls whether the result is saved as a new file or overwrites "
                    "the source in-place.\n"
                    "- true: always create a new file with an incremented suffix "
                    "('_edited.csv', '_edited2.csv', '_edited3.csv', \u2026). "
                    "The system checks existing filenames and picks the first available one.\n"
                    "- false (default for all actions except filter_to_new): overwrite "
                    "the source file in-place when its name already ends with '_edited*.csv'. "
                    "If the source is an original file (no '_edited*' suffix), a new "
                    "'_edited.csv' is always created regardless \u2014 the original is never "
                    "overwritten.\n"
                    "- filter_to_new default: true (materialises a subset into a new file)."
                ),
            },
        },
        "additionalProperties": False,
    }

    audit_field_mapping: ClassVar[dict] = {"target_entities": "file_id"}

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        start = time.monotonic()

        action = params.get("action")
        file_id = params.get("file_id")

        _VALID_ACTIONS = {
            "add_column", "set_column", "delete_rows", "delete_columns",
            "rename_columns", "sort", "deduplicate", "filter_to_new",
            "cast_column", "split_column", "fill_nulls",
        }
        if not isinstance(action, str) or action not in _VALID_ACTIONS:
            return self._failure("INVALID_INPUT", f"'action' must be one of: {sorted(_VALID_ACTIONS)}")
        if not isinstance(file_id, str) or not file_id.strip():
            return self._failure("INVALID_INPUT", "'file_id' is required.")

        delimiter = params.get("delimiter")
        encoding = params.get("encoding")
        if delimiter is not None and (
            not isinstance(delimiter, str) or delimiter not in {",", ";", "\t", "|"}
        ):
            return self._failure(
                "INVALID_INPUT",
                "'delimiter' must be one of: ',', ';', '\\t', '|'.",
            )

        # ── Validate filter / where_sql mutual exclusivity ───────────────
        filter_spec = params.get("filter")
        where_sql_raw = params.get("where_sql")
        if filter_spec is not None and where_sql_raw is not None:
            return self._failure(
                "INVALID_INPUT",
                "'filter' and 'where_sql' are mutually exclusive. Provide at most one.",
            )
        if where_sql_raw is not None:
            err = validate_where_sql(str(where_sql_raw))
            if err:
                return self._failure("INVALID_SQL", err)
            where_sql: Optional[str] = str(where_sql_raw)
        else:
            where_sql = None

        # ── Load CSV ─────────────────────────────────────────────────────
        try:
            df, file_meta = await load_csv(
                self,
                agent_context,
                file_id,
                delimiter=delimiter if isinstance(delimiter, str) else None,
                encoding=encoding if isinstance(encoding, str) else None,
            )
        except FileNotFoundError as exc:
            return self._failure("FILE_NOT_FOUND", str(exc))
        except ValueError as exc:
            return self._failure("PARSE_ERROR", str(exc))
        except Exception as exc:  # pragma: no cover
            logger.exception("csv_edit: unexpected load failure: %s", exc)
            return self._failure("INTERNAL_ERROR", f"Failed to load CSV: {exc}", retryable=True)

        # ── Dispatch ─────────────────────────────────────────────────────
        try:
            result_df = await self._dispatch_action(
                action, df, params, filter_spec, where_sql
            )
        except ValueError as exc:
            return self._failure("INVALID_INPUT", str(exc))
        except Exception as exc:  # pragma: no cover
            logger.exception("csv_edit: action %s failed: %s", action, exc)
            return self._failure("INTERNAL_ERROR", f"Action failed: {exc}")

        # ── Resolve create_new flag ──────────────────────────────────────
        # filter_to_new always defaults to True (its purpose is materialising
        # a subset, not mutating the source).  All other actions default to
        # False (overwrite _edited* in-place; create only when source is plain).
        create_new_param = params.get("create_new")
        create_new: bool = (
            action == "filter_to_new"
            if create_new_param is None
            else bool(create_new_param)
        )

        # ── Persist output ───────────────────────────────────────────────
        original_filename = file_meta.get("filename") or "output.csv"

        csv_bytes = await asyncio.to_thread(df_to_csv_bytes, result_df)

        output_file: Optional[dict] = None
        output_filename: str = original_filename
        is_inplace: bool = False
        try:
            from src.shared.database import _get_session_maker  # noqa: PLC0415

            async with _get_session_maker()() as db_session:
                # Query existing _edited* filenames to find the next free name.
                existing = await _fetch_edited_filenames(
                    org_id=agent_context.org_id,
                    user_id=agent_context.user_id,
                    original_filename=original_filename,
                    session=db_session,
                )
                output_filename, is_inplace = compute_edited_filename(
                    original_filename,
                    force_new=create_new,
                    existing_filenames=existing,
                )

                if is_inplace:
                    output_file = await self._replace_file_content(
                        file_id=str(file_meta.get("file_id", file_id)),
                        data=csv_bytes,
                        agent_context=agent_context,
                        session=db_session,
                    )
                    if output_file is not None:
                        await db_session.commit()
                else:
                    output_file = await self._store_file(
                        data=csv_bytes,
                        filename=output_filename,
                        content_type="text/csv",
                        agent_context=agent_context,
                        session=db_session,
                        description=(
                            f"csv_edit/{action} result from '{original_filename}' "
                            f"({result_df.height} rows, {result_df.width} cols)"
                        ),
                    )
        except Exception as exc:  # pragma: no cover
            logger.warning("csv_edit: could not persist output: %s", exc)

        # Invalidate the loader cache so subsequent reads reflect new content.
        org_id = str(agent_context.org_id)
        out_file_id = (output_file or {}).get("file_id") or str(file_meta.get("file_id", file_id))
        invalidate_cache(org_id, str(file_meta.get("file_id", file_id)))
        if out_file_id != str(file_meta.get("file_id", file_id)):
            invalidate_cache(org_id, out_file_id)

        elapsed = int((time.monotonic() - start) * 1000)
        result_data: dict = {
            "action": action,
            "source_file": {
                "file_id": file_meta.get("file_id"),
                "filename": file_meta.get("filename"),
            },
            "output_file": output_file,
            "is_inplace": is_inplace,
            "output_rows": result_df.height,
            "output_columns": result_df.width,
            "rows_delta": result_df.height - df.height,
        }
        if output_file is None:
            result_data["warning"] = (
                "Output could not be saved to storage. "
                "The edit operation succeeded but the result file is unavailable."
            )
        else:
            hint = (
                "File updated in-place — use the same file_id for further edits."
                if is_inplace
                else (
                    f"New file created: '{output_filename}'. "
                    "Use output_file.file_id for further edits or queries."
                )
            )
            result_data["hint"] = hint

        return self._success(result_data, execution_time_ms=elapsed)

    async def _dispatch_action(
        self,
        action: str,
        df: pl.DataFrame,
        params: dict,
        filter_spec: Optional[dict],
        where_sql: Optional[str],
    ) -> pl.DataFrame:
        """Route to the appropriate synchronous action handler."""

        if action == "add_column":
            column = params.get("column")
            value = params.get("value")
            overwrite = bool(params.get("overwrite", False))
            if not isinstance(column, str) or not column:
                raise ValueError("'column' is required for add_column.")
            if not isinstance(value, dict):
                raise ValueError("'value' must be a value source object.")
            return await asyncio.to_thread(
                _action_add_column, df, column=column, value=value, overwrite=overwrite
            )

        if action == "set_column":
            column = params.get("column")
            value = params.get("value")
            if not isinstance(column, str) or not column:
                raise ValueError("'column' is required for set_column.")
            if not isinstance(value, dict):
                raise ValueError("'value' must be a value source object.")
            return await asyncio.to_thread(
                _action_set_column,
                df,
                column=column,
                value=value,
                filter_spec=filter_spec,
                where_sql=where_sql,
            )

        if action == "delete_rows":
            if filter_spec is None and where_sql is None:
                raise ValueError(
                    "delete_rows requires 'filter' or 'where_sql' to identify rows to remove."
                )
            return await asyncio.to_thread(
                _action_delete_rows, df, filter_spec=filter_spec, where_sql=where_sql
            )

        if action == "delete_columns":
            columns = params.get("columns")
            if not isinstance(columns, list) or not columns:
                raise ValueError("'columns' must be a non-empty list for delete_columns.")
            if not all(isinstance(c, str) for c in columns):
                raise ValueError("'columns' must be a list of strings.")
            return await asyncio.to_thread(_action_delete_columns, df, columns=columns)

        if action == "rename_columns":
            column_map = params.get("column_map")
            if not isinstance(column_map, dict) or not column_map:
                raise ValueError("'column_map' must be a non-empty object for rename_columns.")
            if not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in column_map.items()
            ):
                raise ValueError("'column_map' must map strings to strings.")
            return await asyncio.to_thread(
                _action_rename_columns, df, column_map=column_map
            )

        if action == "sort":
            by = params.get("by")
            if not isinstance(by, list) or not by:
                raise ValueError("'by' must be a non-empty list for sort.")
            return await asyncio.to_thread(_action_sort, df, by=by)

        if action == "deduplicate":
            subset = params.get("subset")
            keep = str(params.get("keep", "first"))
            if subset is not None and (
                not isinstance(subset, list) or not all(isinstance(c, str) for c in subset)
            ):
                raise ValueError("'subset' must be a list of strings.")
            return await asyncio.to_thread(
                _action_deduplicate, df, subset=subset, keep=keep
            )

        if action == "filter_to_new":
            if filter_spec is None and where_sql is None:
                raise ValueError(
                    "filter_to_new requires 'filter' or 'where_sql' to identify rows to keep."
                )
            return await asyncio.to_thread(
                _action_filter_to_new, df, filter_spec=filter_spec, where_sql=where_sql
            )

        if action == "cast_column":
            column = params.get("column")
            target_type = params.get("target_type")
            date_format = params.get("date_format")
            strict = bool(params.get("strict", False))
            if not isinstance(column, str) or not column:
                raise ValueError("'column' is required for cast_column.")
            if not isinstance(target_type, str):
                raise ValueError("'target_type' is required for cast_column.")
            return await asyncio.to_thread(
                _action_cast_column,
                df,
                column=column,
                target_type=target_type,
                date_format=date_format if isinstance(date_format, str) else None,
                strict=strict,
            )

        if action == "split_column":
            column = params.get("column")
            separator = params.get("separator")
            pattern = params.get("pattern")
            new_columns = params.get("new_columns")
            n = params.get("n")
            drop_original = bool(params.get("drop_original", False))
            if not isinstance(column, str) or not column:
                raise ValueError("'column' is required for split_column.")
            if separator is None and pattern is None:
                raise ValueError("split_column requires 'separator' or 'pattern'.")
            if n is None:
                raise ValueError("'n' (number of parts) is required for split_column.")
            if not isinstance(n, int) or n < 2:
                raise ValueError("'n' must be an integer >= 2.")
            if new_columns is not None and not all(isinstance(c, str) for c in new_columns):
                raise ValueError("'new_columns' must be a list of strings.")
            return await asyncio.to_thread(
                _action_split_column,
                df,
                column=column,
                separator=separator if isinstance(separator, str) else None,
                pattern=pattern if isinstance(pattern, str) else None,
                new_columns=new_columns,
                n=n,
                drop_original=drop_original,
            )

        if action == "fill_nulls":
            column = params.get("column")
            value = params.get("value")
            if not isinstance(column, str) or not column:
                raise ValueError("'column' is required for fill_nulls.")
            if not isinstance(value, dict):
                raise ValueError("'value' must be a value source object for fill_nulls.")
            return await asyncio.to_thread(
                _action_fill_nulls,
                df,
                column=column,
                value=value,
                filter_spec=filter_spec,
                where_sql=where_sql,
            )

        raise ValueError(f"Unhandled action: {action!r}")  # unreachable
