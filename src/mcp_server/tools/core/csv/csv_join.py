"""csv_join — join two or more stored CSV files via DuckDB.

Supports two complementary modes so the agent can pick whichever is more
natural for the task at hand:

* **Structured mode** — provide ``files`` (each with an ``alias``) plus a
  ``joins`` array describing each pairwise join (left/right alias, join
  keys, join type). The tool builds the SQL automatically and aliases
  conflicting non-key column names with ``__<alias>`` suffixes.
* **SQL mode** — provide ``files`` (each with an ``alias``) plus a raw
  ``sql`` SELECT statement that references the aliases as DuckDB views.
  Useful for complex joins with subqueries, CTEs (``WITH ...``),
  ``UNION`` / ``UNION ALL`` / ``INTERSECT`` / ``EXCEPT`` set operations,
  expression-based conditions, deduplication via ``DISTINCT`` or
  ``QUALIFY ROW_NUMBER()``, etc. Same sandbox rules as ``csv_query``.

Each CSV is loaded via the shared :func:`load_csv` (MinIO + cache + access
control) and registered with DuckDB as a zero-copy Arrow view named after
its alias. Up to 5 files per call.

The full result is **always persisted** as a new CSV in MinIO; only a
small inline preview (50 rows) is returned to the LLM agent.

Permission: ``core:csv_join``
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, ClassVar, Optional

import duckdb  # type: ignore[import-untyped]
import polars as pl

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.core.csv.csv_loader import access_error_code, load_csv
from src.mcp_server.tools.core.csv.csv_shared import (
    df_to_csv_bytes,
    validate_sql_safe as _validate_sql,
)
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ── Safety limits ──────────────────────────────────────────────────────────
_QUERY_TIMEOUT_SECONDS: float = 30.0
_MAX_FILES: int = 5
_MAX_RESULT_ROWS: int = 1_000_000
_MEMORY_LIMIT: str = "1GB"
# Inline preview budget returned to the LLM agent.
_MAX_INLINE_ROWS: int = 50
_MAX_INLINE_BYTES: int = 16_000

# Valid join types (DuckDB syntax).
_JOIN_TYPES: dict[str, str] = {
    "left": "LEFT JOIN",
    "right": "RIGHT JOIN",
    "inner": "INNER JOIN",
    "full": "FULL OUTER JOIN",
}

# Alias / identifier validation — DuckDB-safe unquoted identifier.
_IDENT_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _q_ident(name: str) -> str:
    """Quote a DuckDB identifier.

    Uses double quotes and escapes embedded quotes.  Applied to column names
    only — aliases are pre-validated against :data:`_IDENT_RE`.
    """
    return '"' + name.replace('"', '""') + '"'


def _run_duckdb_join(
    arrow_tables: list[tuple[str, Any]], sql: str
) -> tuple[list[str], list[list]]:
    """Execute ``sql`` against multiple Arrow tables registered by alias.

    Synchronous helper; call via ``asyncio.to_thread``.
    """
    con = duckdb.connect(database=":memory:")
    try:
        # Lock down the in-process DB before any user input is evaluated.
        con.execute("SET disabled_filesystems='LocalFileSystem'")
        con.execute(f"SET memory_limit='{_MEMORY_LIMIT}'")
        con.execute("SET threads TO 2")
        for alias, table in arrow_tables:
            con.register(alias, table)

        cur = con.execute(sql)
        columns = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(_MAX_RESULT_ROWS + 1)
        return columns, [list(r) for r in rows]
    finally:
        try:
            con.close()
        except Exception:
            pass


def _build_join_sql(
    file_specs: list[dict],
    joins: list[dict],
) -> str:
    """Build a DuckDB SELECT...JOIN...ON SQL string for the structured mode.

    Parameters
    ----------
    file_specs:
        Validated ``[{"alias": str, "columns": list[str], "join_keys": set[str]}, …]``.
    joins:
        Validated ``[{"left": str, "right": str, "left_on": list[str],
        "right_on": list[str], "how": "left"|…}, …]``.

    The select list aliases non-key columns to ``"<col>__<alias>"`` whenever a
    name collides with one already chosen.  Join-key columns from the *right*
    side of each join are *not* emitted (they would duplicate the matching
    left-side key).
    """
    # Track right-side keys we suppress (they're redundant).
    suppressed: dict[str, set[str]] = {fs["alias"]: set() for fs in file_specs}
    for j in joins:
        suppressed[j["right"]].update(j["right_on"])

    # Build the SELECT projection in order, suffixing collisions.
    seen_names: set[str] = set()
    select_parts: list[str] = []
    for fs in file_specs:
        alias = fs["alias"]
        for col in fs["columns"]:
            if col in suppressed[alias]:
                continue
            if col in seen_names:
                out_name = f"{col}__{alias}"
                # Extremely unlikely fallback if suffixing also collides.
                while out_name in seen_names:
                    out_name = f"{out_name}_"
            else:
                out_name = col
            seen_names.add(out_name)
            select_parts.append(
                f"{_q_ident(alias)}.{_q_ident(col)} AS {_q_ident(out_name)}"
            )

    # FROM / JOIN chain.  joins[0].left must be file_specs[0].alias; each
    # subsequent join brings exactly one new alias on its right.
    from_clause = f"FROM {_q_ident(file_specs[0]['alias'])}"
    join_clauses: list[str] = []
    for j in joins:
        keyword = _JOIN_TYPES[j["how"]]
        on_parts = [
            f"{_q_ident(j['left'])}.{_q_ident(lk)} = {_q_ident(j['right'])}.{_q_ident(rk)}"
            for lk, rk in zip(j["left_on"], j["right_on"])
        ]
        on_clause = " AND ".join(on_parts)
        join_clauses.append(f"{keyword} {_q_ident(j['right'])} ON {on_clause}")

    return (
        "SELECT " + ", ".join(select_parts) + " "
        + from_clause + " " + " ".join(join_clauses)
    )


def _validate_structured(
    file_specs: list[dict],
    joins_raw: Any,
) -> tuple[Optional[list[dict]], Optional[str]]:
    """Validate the ``joins`` parameter against the resolved file specs.

    Returns ``(validated_joins, None)`` on success or ``(None, error_msg)``
    on failure.  ``validated_joins`` is a list of dicts shaped like
    ``{"left": str, "right": str, "left_on": list[str], "right_on": list[str],
    "how": "left"|"right"|"inner"|"full"}``.
    """
    if not isinstance(joins_raw, list) or not joins_raw:
        return None, "'joins' must be a non-empty list."
    if len(joins_raw) != len(file_specs) - 1:
        return None, (
            f"'joins' must have exactly {len(file_specs) - 1} entries "
            f"(one per additional file). Got {len(joins_raw)}."
        )

    alias_by_index: dict[str, int] = {fs["alias"]: i for i, fs in enumerate(file_specs)}
    introduced: set[str] = {file_specs[0]["alias"]}
    validated: list[dict] = []

    for idx, j in enumerate(joins_raw):
        if not isinstance(j, dict):
            return None, f"joins[{idx}] must be an object."

        left = j.get("left")
        right = j.get("right")
        how = j.get("how", "left")
        left_on = j.get("left_on")
        right_on = j.get("right_on")

        if not isinstance(left, str) or left not in alias_by_index:
            return None, (
                f"joins[{idx}].left must reference one of the file aliases."
            )
        if not isinstance(right, str) or right not in alias_by_index:
            return None, (
                f"joins[{idx}].right must reference one of the file aliases."
            )
        if left == right:
            return None, f"joins[{idx}].left and .right must differ."
        if left not in introduced:
            return None, (
                f"joins[{idx}].left='{left}' has not been introduced yet. "
                "Each join's 'left' must reference a file already brought into "
                "the chain (the first file is implicit)."
            )
        if right in introduced:
            return None, (
                f"joins[{idx}].right='{right}' has already been joined. Each "
                "additional file may appear on the right of exactly one join."
            )
        if not isinstance(how, str) or how not in _JOIN_TYPES:
            return None, (
                f"joins[{idx}].how must be one of: {sorted(_JOIN_TYPES)}."
            )

        # Normalise left_on / right_on to lists of strings.
        def _norm(v: Any) -> Optional[list[str]]:
            if isinstance(v, str):
                return [v]
            if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                return list(v)
            return None

        lon = _norm(left_on)
        ron = _norm(right_on)
        if lon is None or ron is None:
            return None, (
                f"joins[{idx}].left_on and .right_on must be strings or "
                "non-empty lists of strings."
            )
        if len(lon) != len(ron):
            return None, (
                f"joins[{idx}]: left_on and right_on must have the same length."
            )

        # Verify columns exist in each side.
        left_cols = set(file_specs[alias_by_index[left]]["columns"])
        right_cols = set(file_specs[alias_by_index[right]]["columns"])
        missing_l = [c for c in lon if c not in left_cols]
        missing_r = [c for c in ron if c not in right_cols]
        if missing_l:
            return None, (
                f"joins[{idx}]: left_on columns not found in '{left}': {missing_l}."
            )
        if missing_r:
            return None, (
                f"joins[{idx}]: right_on columns not found in '{right}': {missing_r}."
            )

        introduced.add(right)
        validated.append({
            "left": left,
            "right": right,
            "left_on": lon,
            "right_on": ron,
            "how": how,
        })

    return validated, None


class CsvJoinTool(BaseTool):
    """Join two or more stored CSV files via DuckDB.

    Two modes are exposed; provide exactly one:

    **Structured mode** — pass ``files`` and ``joins``.  The tool builds
    the SQL for you and resolves column-name collisions automatically by
    appending ``__<alias>`` to clashing column names from later files.
    Each join entry describes one pairwise join:
    ``{"left": alias_already_introduced, "right": new_alias, "left_on":
    cols, "right_on": cols, "how": "left"|"right"|"inner"|"full"}``.
    The number of joins must equal ``len(files) - 1`` and each additional
    file appears on the right of exactly one join.

    **SQL mode** — pass ``files`` and ``sql``.  Each file is registered
    under its alias as a DuckDB view (``csv`` is the default name when
    no alias is given for a single file, otherwise aliases are required).
    Same sandbox rules as ``csv_query``: only ``SELECT`` / ``WITH``,
    no DDL / DML / filesystem functions.

    SQL mode is not limited to joins — any read-only DuckDB query that
    combines the registered views is allowed. Common patterns:

    * **Vertical stack (UNION ALL)** — append rows from multiple files
      that share a schema::

          SELECT * FROM t1
          UNION ALL
          SELECT * FROM t2

      Use plain ``UNION`` to also deduplicate identical rows.

    * **Stack + deduplicate by key** — keep the first occurrence of
      each IP across files::

          WITH stacked AS (
              SELECT *, 't1' AS __src FROM t1
              UNION ALL BY NAME
              SELECT *, 't2' AS __src FROM t2
          )
          SELECT * FROM stacked
          QUALIFY ROW_NUMBER() OVER (PARTITION BY ip ORDER BY __src) = 1

      ``UNION ALL BY NAME`` aligns columns by name (filling missing
      columns with NULL) instead of by position — handy when files
      have overlapping but not identical schemas.

    * **Set difference / intersection** — ``EXCEPT`` and ``INTERSECT``
      also work directly between the aliased views.

    * **CTEs** — ``WITH cte AS (...) SELECT ... FROM cte JOIN t2 ...``
      is fully supported and is the recommended way to structure
      multi-step transformations.

    Hard limits:

    * Up to 5 files per call.
    * ``SET disabled_filesystems='LocalFileSystem'`` — no host file access.
    * ``memory_limit=1GB`` — bounded RAM.
    * ``timeout_seconds=60`` (class-level) — wall-clock cap.
    * Up to 1 000 000 result rows are kept in memory.

    The full result is **always saved** as a new CSV in MinIO and surfaced
    via ``generated_file`` (with ``file_id`` and ``download_path``).  The
    inline payload is a small preview (50 rows / ~16 KB).

    Permission: ``core:csv_join``
    """

    name: ClassVar[str] = "csv_join"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Left/right/inner/full join across two or more stored CSV files."
    )
    category: ClassVar[str] = "data"
    permissions: ClassVar[list[str]] = ["core:csv_join"]
    rate_limit_per_minute: ClassVar[int] = 200
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["files"],
        "properties": {
            "files": {
                "type": "array",
                "minItems": 2,
                "maxItems": _MAX_FILES,
                "description": (
                    f"List of CSV files to join (2 to {_MAX_FILES}). Each "
                    "entry has the file's UUID and a short alias used to "
                    "reference its columns in 'joins' or 'sql'. "
                    "Aliases must match ^[a-zA-Z_][a-zA-Z0-9_]*$ and be "
                    "unique. If omitted, aliases default to t1, t2, …"
                ),
                "items": {
                    "type": "object",
                    "required": ["file_id"],
                    "properties": {
                        "file_id": {
                            "type": "string",
                            "description": "UUID of the CSV file (GSageFile.id).",
                        },
                        "alias": {
                            "type": "string",
                            "description": (
                                "Identifier used in 'joins' / 'sql'. "
                                "Defaults to t1, t2, … in array order."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "joins": {
                "type": "array",
                "description": (
                    "Structured mode: describe each pairwise join. Required "
                    "if 'sql' is not provided. Length must equal len(files) - 1. "
                    "Each item: {left, right, left_on, right_on, how}. "
                    "'left' must reference an alias already introduced; "
                    "'right' must be the next new alias. 'how' is one of "
                    "'left', 'right', 'inner', 'full'."
                ),
                "items": {
                    "type": "object",
                    "required": ["left", "right", "left_on", "right_on"],
                    "properties": {
                        "left": {"type": "string"},
                        "right": {"type": "string"},
                        "left_on": {
                            "oneOf": [
                                {"type": "string"},
                                {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 1,
                                },
                            ],
                        },
                        "right_on": {
                            "oneOf": [
                                {"type": "string"},
                                {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 1,
                                },
                            ],
                        },
                        "how": {
                            "type": "string",
                            "enum": list(_JOIN_TYPES.keys()),
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "sql": {
                "type": "string",
                "description": (
                    "SQL mode: a single DuckDB read-only statement that "
                    "references the file aliases as views. Provide instead "
                    "of 'joins'. Supports SELECT / WITH (CTEs), JOINs, "
                    "UNION / UNION ALL [BY NAME] / INTERSECT / EXCEPT, "
                    "subqueries, window functions and QUALIFY for "
                    "deduplication. Use 'UNION ALL' to stack rows from "
                    "multiple files vertically (same schema) and "
                    "'UNION ALL BY NAME' when files have overlapping but "
                    "not identical columns. Only one statement; DDL / DML "
                    "/ filesystem functions are rejected."
                ),
            },
            "output_filename": {
                "type": "string",
                "description": (
                    "Custom filename for the output CSV (e.g. 'joined.csv'). "
                    "Defaults to 'join_result.csv'."
                ),
            },
            "delimiter": {
                "type": "string",
                "description": (
                    "Override delimiter detection for ALL input files. "
                    "Allowed values: ',', ';', '\\t', '|'. Omit for "
                    "per-file auto-detect."
                ),
            },
            "encoding": {
                "type": "string",
                "description": (
                    "Override encoding detection for ALL input files."
                ),
            },
        },
        "additionalProperties": False,
    }

    audit_field_mapping: ClassVar[dict] = {"target_entities": "files"}

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        start = time.monotonic()

        # ── Parameter sanity ───────────────────────────────────────────────
        files_param = params.get("files")
        if not isinstance(files_param, list) or len(files_param) < 2:
            return self._failure(
                "INVALID_INPUT", "'files' must list at least two CSV files."
            )
        if len(files_param) > _MAX_FILES:
            return self._failure(
                "INVALID_INPUT",
                f"At most {_MAX_FILES} files may be joined per call.",
            )

        # Build the alias/file_id list, defaulting aliases to t1, t2, …
        seen_aliases: set[str] = set()
        seen_file_ids: set[str] = set()
        files_info: list[dict] = []
        for idx, entry in enumerate(files_param):
            if not isinstance(entry, dict):
                return self._failure(
                    "INVALID_INPUT", f"files[{idx}] must be an object."
                )
            fid = entry.get("file_id")
            if not isinstance(fid, str) or not fid.strip():
                return self._failure(
                    "INVALID_INPUT", f"files[{idx}].file_id is required."
                )
            alias = entry.get("alias") or f"t{idx + 1}"
            if not isinstance(alias, str) or not _IDENT_RE.match(alias):
                return self._failure(
                    "INVALID_INPUT",
                    f"files[{idx}].alias must match ^[a-zA-Z_][a-zA-Z0-9_]*$.",
                )
            if alias in seen_aliases:
                return self._failure(
                    "INVALID_INPUT", f"Duplicate alias: '{alias}'."
                )
            if fid in seen_file_ids:
                return self._failure(
                    "INVALID_INPUT",
                    f"Duplicate file_id in 'files': '{fid}'. Each file may "
                    "appear at most once.",
                )
            seen_aliases.add(alias)
            seen_file_ids.add(fid)
            files_info.append({"file_id": fid.strip(), "alias": alias})

        # Validate output filename.
        output_filename = str(params.get("output_filename") or "join_result.csv").strip()
        if not output_filename.lower().endswith(".csv"):
            output_filename += ".csv"

        # Validate delimiter / encoding (applied to every file).
        delimiter = params.get("delimiter")
        encoding = params.get("encoding")
        if delimiter is not None and (
            not isinstance(delimiter, str) or delimiter not in {",", ";", "\t", "|"}
        ):
            return self._failure(
                "INVALID_INPUT",
                "'delimiter' must be one of: ',', ';', '\\t', '|'.",
            )
        if encoding is not None and not isinstance(encoding, str):
            return self._failure("INVALID_INPUT", "'encoding' must be a string.")

        # Mode selection: structured vs SQL.
        sql_param = params.get("sql")
        joins_param = params.get("joins")
        if sql_param is not None and joins_param is not None:
            return self._failure(
                "INVALID_INPUT",
                "Provide either 'joins' (structured mode) or 'sql' (raw mode), not both.",
            )
        if sql_param is None and joins_param is None:
            return self._failure(
                "INVALID_INPUT",
                "Provide either 'joins' (structured mode) or 'sql' (raw mode).",
            )

        mode: str
        if sql_param is not None:
            if not isinstance(sql_param, str) or not sql_param.strip():
                return self._failure("INVALID_INPUT", "'sql' must be a non-empty string.")
            validation_error = _validate_sql(sql_param)
            if validation_error is not None:
                return self._failure("INVALID_SQL", validation_error)
            mode = "sql"
        else:
            mode = "structured"

        # ── Load all files in parallel via the shared loader ──────────────
        async def _load_one(info: dict) -> tuple[dict, pl.DataFrame, dict]:
            df, meta = await load_csv(
                self,
                agent_context,
                info["file_id"],
                delimiter=delimiter if isinstance(delimiter, str) else None,
                encoding=encoding if isinstance(encoding, str) else None,
            )
            return info, df, meta

        try:
            load_results = await asyncio.gather(*(_load_one(i) for i in files_info))
        except FileNotFoundError as exc:
            return self._failure(access_error_code(exc), str(exc))
        except ValueError as exc:
            return self._failure("PARSE_ERROR", str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("csv_join: unexpected load failure: %s", exc)
            return self._failure(
                "INTERNAL_ERROR", f"Failed to load CSV: {exc}", retryable=True
            )

        # Build the per-file context lists used downstream.
        file_specs: list[dict] = []
        arrow_tables: list[tuple[str, Any]] = []
        file_metas: list[dict] = []
        for info, df, meta in load_results:
            file_specs.append({
                "alias": info["alias"],
                "columns": list(df.columns),
            })
            try:
                arrow_tables.append((info["alias"], df.to_arrow()))
            except Exception as exc:  # pragma: no cover - defensive
                return self._failure(
                    "INTERNAL_ERROR",
                    f"Failed to convert '{info['alias']}' to Arrow: {exc}",
                )
            file_metas.append({
                "alias": info["alias"],
                "file_id": meta.get("file_id"),
                "filename": meta.get("filename"),
                "rows": meta.get("rows"),
                "columns": meta.get("columns"),
            })

        # ── Build the SQL ─────────────────────────────────────────────────
        if mode == "structured":
            validated_joins, err = _validate_structured(file_specs, joins_param)
            if validated_joins is None:
                return self._failure("INVALID_INPUT", err or "Invalid 'joins'.")
            try:
                sql = _build_join_sql(file_specs, validated_joins)
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("csv_join: SQL build failed: %s", exc)
                return self._failure(
                    "INTERNAL_ERROR", f"Failed to build join SQL: {exc}"
                )
        else:
            assert sql_param is not None  # guarded above by mode == "sql" branch
            sql = sql_param

        # ── Execute ───────────────────────────────────────────────────────
        try:
            columns, raw_rows = await asyncio.wait_for(
                asyncio.to_thread(_run_duckdb_join, arrow_tables, sql),
                timeout=_QUERY_TIMEOUT_SECONDS + 2.0,
            )
        except asyncio.TimeoutError:
            return self._failure(
                "QUERY_TIMEOUT",
                f"Join query exceeded the {int(_QUERY_TIMEOUT_SECONDS)}s timeout.",
                retryable=False,
            )
        except duckdb.Error as exc:  # type: ignore[attr-defined]
            return self._failure("SQL_ERROR", str(exc))
        except Exception as exc:
            logger.exception("csv_join: unexpected DuckDB failure: %s", exc)
            return self._failure(
                "INTERNAL_ERROR", f"Join execution failed: {exc}"
            )

        truncated_rows = len(raw_rows) > _MAX_RESULT_ROWS
        if truncated_rows:
            raw_rows = raw_rows[:_MAX_RESULT_ROWS]
        total_rows = len(raw_rows)

        # ── Persist full result as a CSV in MinIO (always) ────────────────
        generated_file: Optional[dict] = None
        try:
            result_df = pl.DataFrame(
                {col: [r[i] for r in raw_rows] for i, col in enumerate(columns)},
                strict=False,
            )
            csv_bytes = await asyncio.to_thread(df_to_csv_bytes, result_df)

            from src.shared.database import _get_session_maker  # noqa: PLC0415

            source_filenames = ", ".join(
                f"'{m.get('filename')}'" for m in file_metas if m.get("filename")
            )
            async with _get_session_maker()() as db_session:
                generated_file = await self._store_file(
                    data=csv_bytes,
                    filename=output_filename,
                    content_type="text/csv",
                    agent_context=agent_context,
                    session=db_session,
                    description=(
                        f"csv_join result from {source_filenames} "
                        f"({result_df.height} rows, {result_df.width} cols)"
                    ),
                )
            if generated_file is None:
                logger.warning(
                    "csv_join: _store_file returned None for output '%s'",
                    output_filename,
                )
        except Exception as exc:  # pragma: no cover - storage issues are non-fatal
            logger.warning("csv_join: could not persist output file: %s", exc)

        # ── Build inline preview (always trimmed) ─────────────────────────
        inline_raw_rows = raw_rows[:_MAX_INLINE_ROWS]
        rows_dicts = [dict(zip(columns, r)) for r in inline_raw_rows]
        preview_trimmed_rows = total_rows > len(inline_raw_rows)

        result_payload: dict = {
            "columns": columns,
            "rows": rows_dicts,
            "row_count": total_rows,
            "preview_row_count": len(rows_dicts),
            "truncated_rows": truncated_rows or preview_trimmed_rows,
            "truncated_bytes": False,
        }
        serialised = json.dumps(result_payload, ensure_ascii=False, default=str)
        while (
            len(serialised.encode("utf-8")) > _MAX_INLINE_BYTES
            and len(result_payload["rows"]) > 1
        ):
            result_payload["rows"] = result_payload["rows"][
                : max(1, len(result_payload["rows"]) // 2)
            ]
            result_payload["preview_row_count"] = len(result_payload["rows"])
            result_payload["truncated_rows"] = True
            result_payload["truncated_bytes"] = True
            serialised = json.dumps(result_payload, ensure_ascii=False, default=str)

        elapsed = int((time.monotonic() - start) * 1000)
        result_data: dict = {
            "mode": mode,
            "files": file_metas,
            "sql": sql,
            "result": result_payload,
        }
        if generated_file is not None:
            result_data["generated_file"] = generated_file
            result_data["notice"] = (
                f"Join result has {total_rows} rows. The full result was "
                "saved as a CSV file — see 'generated_file' for the download "
                "path / file_id. Inline preview shows up to "
                f"{_MAX_INLINE_ROWS} rows."
            )
        else:
            result_data["warning"] = (
                "Join completed but the result file could not be saved. "
                "Inline preview only; full result is unavailable."
            )
        return self._success(result_data, execution_time_ms=elapsed)


__all__ = ["CsvJoinTool"]
