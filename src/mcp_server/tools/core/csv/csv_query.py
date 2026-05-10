"""csv_query — run SELECT-only DuckDB SQL over a stored CSV file.

The CSV is parsed once via the shared loader and registered with DuckDB as
an Arrow zero-copy view named ``csv``.  Only ``SELECT`` / ``WITH`` queries
are accepted; any DDL / DML / filesystem statements are rejected before the
SQL is sent to DuckDB.

Permission: ``core:csv_query``
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import ClassVar, Optional

import duckdb  # type: ignore[import-untyped]

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.core.csv.csv_loader import load_csv, result_to_payload
from src.mcp_server.tools.core.csv.csv_shared import (
    _COMMENT_RE,
    _DENY_FUNCTION_PATTERNS,
    _DENY_PATTERNS,
    _strip_comments,
    validate_sql_safe as _validate_sql,
)
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ── Safety limits ──────────────────────────────────────────────────────────
_QUERY_TIMEOUT_SECONDS: float = 10.0
_MAX_RESULT_ROWS: int = 5000
_MEMORY_LIMIT: str = "256MB"
_MAX_INLINE_BYTES: int = 50_000


def _run_duckdb_query(arrow_table, sql: str) -> tuple[list[str], list[list]]:
    """Execute ``sql`` against an Arrow table registered as ``csv``.

    Synchronous helper; called from a worker thread.
    """
    con = duckdb.connect(database=":memory:")
    try:
        # Lock down the in-process DB before any user input is evaluated.
        con.execute("SET disabled_filesystems='LocalFileSystem'")
        con.execute(f"SET memory_limit='{_MEMORY_LIMIT}'")
        con.execute("SET threads TO 2")
        # DuckDB has no SQL-level query timeout setting.  Wall-clock protection
        # is provided by the tool's timeout_seconds class attribute, which
        # wraps the call in a background thread with a hard deadline.
        con.register("csv", arrow_table)

        cur = con.execute(sql)
        columns = [d[0] for d in (cur.description or [])]
        # Always cap rows server-side.
        rows = cur.fetchmany(_MAX_RESULT_ROWS + 1)
        return columns, [list(r) for r in rows]
    finally:
        try:
            con.close()
        except Exception:
            pass


class CsvQueryTool(BaseTool):
    """Run a SELECT-only DuckDB SQL query over a stored CSV file.

    The CSV (referenced by ``file_id``) is parsed via the shared loader and
    exposed inside DuckDB as a view named ``csv``.  Only ``SELECT`` and
    ``WITH ... SELECT`` queries are accepted; DDL/DML and filesystem-related
    statements / functions are rejected before the SQL is sent to the engine.

    Hard limits applied to every query:

    * ``SET disabled_filesystems='LocalFileSystem'`` — no host file access.
    * ``memory_limit=256MB`` — bounded RAM per query.
    * ``timeout_seconds=30`` (class-level) — wall-clock cap enforced by the
      tool runner; DuckDB itself has no SQL-level timeout setting.
    * Up to 5 000 result rows are returned; anything beyond is truncated.

    Use ``csv_describe`` first to discover columns; this tool returns rows
    as a list of dicts, with rich preview / truncation metadata.

    Permission: ``core:csv_query``
    """

    name: ClassVar[str] = "csv_query"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Run a SELECT-only DuckDB SQL query over a stored CSV file."
    )
    category: ClassVar[str] = "data"
    permissions: ClassVar[list[str]] = ["core:csv_query"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["file_id", "sql"],
        "properties": {
            "file_id": {
                "type": "string",
                "description": "UUID of the CSV file (GSageFile.id).",
            },
            "sql": {
                "type": "string",
                "description": (
                    "DuckDB SELECT statement. The CSV is registered as a "
                    "view named 'csv' (e.g. SELECT col_a, COUNT(*) FROM csv "
                    "GROUP BY col_a). Only one statement per call. "
                    "DDL / DML / filesystem functions are rejected."
                ),
            },
            "delimiter": {
                "type": "string",
                "description": (
                    "Override delimiter detection. Allowed values: ',', ';', "
                    "'\\t', '|'. Omit for auto-detect."
                ),
            },
            "encoding": {
                "type": "string",
                "description": (
                    "Override encoding detection (e.g. 'utf-8', 'latin-1')."
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

        file_id = params.get("file_id")
        sql = params.get("sql")
        if not isinstance(file_id, str) or not file_id.strip():
            return self._failure("INVALID_INPUT", "'file_id' is required.")
        if not isinstance(sql, str) or not sql.strip():
            return self._failure("INVALID_INPUT", "'sql' is required.")

        validation_error = _validate_sql(sql)
        if validation_error is not None:
            return self._failure("INVALID_SQL", validation_error)

        delimiter = params.get("delimiter")
        encoding = params.get("encoding")
        if delimiter is not None and (
            not isinstance(delimiter, str) or delimiter not in {",", ";", "\t", "|"}
        ):
            return self._failure(
                "INVALID_INPUT",
                "'delimiter' must be one of: ',', ';', '\\t', '|'.",
            )

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
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("csv_query: unexpected load failure: %s", exc)
            return self._failure("INTERNAL_ERROR", f"Failed to load CSV: {exc}", retryable=True)

        # Convert to Arrow once (zero-copy register inside DuckDB).
        try:
            arrow_table = df.to_arrow()
        except Exception as exc:  # pragma: no cover - defensive
            return self._failure("INTERNAL_ERROR", f"Failed to convert frame to Arrow: {exc}")

        try:
            columns, raw_rows = await asyncio.wait_for(
                asyncio.to_thread(_run_duckdb_query, arrow_table, sql),
                timeout=_QUERY_TIMEOUT_SECONDS + 2.0,
            )
        except asyncio.TimeoutError:
            return self._failure(
                "QUERY_TIMEOUT",
                f"SQL query exceeded the {int(_QUERY_TIMEOUT_SECONDS)}s timeout.",
                retryable=False,
            )
        except duckdb.Error as exc:  # type: ignore[attr-defined]
            return self._failure("SQL_ERROR", str(exc))
        except Exception as exc:
            logger.exception("csv_query: unexpected DuckDB failure: %s", exc)
            return self._failure("INTERNAL_ERROR", f"Query execution failed: {exc}")

        truncated_rows = len(raw_rows) > _MAX_RESULT_ROWS
        if truncated_rows:
            raw_rows = raw_rows[:_MAX_RESULT_ROWS]

        rows_dicts = [dict(zip(columns, r)) for r in raw_rows]

        # Build inline payload, trim rows if it busts the inline budget.
        import json

        result_payload: dict = {
            "columns": columns,
            "rows": rows_dicts,
            "row_count": len(rows_dicts),
            "truncated_rows": truncated_rows,
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
            result_payload["truncated_rows"] = True
            result_payload["truncated_bytes"] = True
            serialised = json.dumps(result_payload, ensure_ascii=False, default=str)

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {
                "file": {
                    "file_id": file_meta.get("file_id"),
                    "filename": file_meta.get("filename"),
                    "rows": file_meta.get("rows"),
                    "columns": file_meta.get("columns"),
                },
                "sql": sql,
                "result": result_payload,
            },
            execution_time_ms=elapsed,
        )


# Re-export to keep import-time cost low — payload helper is only used in
# tests / future expansion.
__all__ = ["CsvQueryTool", "result_to_payload"]
