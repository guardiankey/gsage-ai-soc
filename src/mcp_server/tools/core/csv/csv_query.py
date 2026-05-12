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
import polars as pl

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.core.csv.csv_loader import access_error_code, load_csv, result_to_payload
from src.mcp_server.tools.core.csv.csv_shared import (
    _COMMENT_RE,
    _DENY_FUNCTION_PATTERNS,
    _DENY_PATTERNS,
    _strip_comments,
    df_to_csv_bytes,
    validate_sql_safe as _validate_sql,
)
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

# ── Safety limits ──────────────────────────────────────────────────────────
_QUERY_TIMEOUT_SECONDS: float = 10.0
# Maximum number of rows materialised from DuckDB into Python (hard cap on
# the full result set kept in memory, before optional persistence to CSV).
_MAX_RESULT_ROWS: int = 1_000_000
_MEMORY_LIMIT: str = "1GB"
# Inline preview budget returned to the LLM agent.  Both caps act together;
# whichever triggers first causes the preview to be trimmed.  When the full
# result exceeds either limit and ``output_file`` was not explicitly set, a
# CSV is auto-generated so the user can download / reference it.
_MAX_INLINE_ROWS: int = 50
_MAX_INLINE_BYTES: int = 16_000


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
    * ``memory_limit=1GB`` — bounded RAM per query.
    * ``timeout_seconds=30`` (class-level) — wall-clock cap enforced by the
      tool runner; DuckDB itself has no SQL-level timeout setting.
    * Up to 1 000 000 result rows are kept in memory; anything beyond is
      truncated.  The inline preview returned to the LLM is capped at
      50 rows (and ~16 KB of serialised JSON).

    When the full result exceeds the inline preview limit, the tool
    **automatically** saves the complete result as a CSV file in MinIO
    and returns its ``file_id`` / ``download_path`` in ``generated_file``,
    along with a ``notice`` field describing the truncation.

    Set ``output_file=true`` to force saving the full query result as a
    new CSV file even when it would fit inline.

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
    rate_limit_per_minute: ClassVar[int] = 300
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
            "output_file": {
                "type": "boolean",
                "description": (
                    "Force saving the full query result as a new CSV file "
                    "in MinIO, even when the result would fit inline. "
                    "Results larger than the inline preview (~50 rows) are "
                    "auto-saved regardless of this flag. The response "
                    "includes a 'generated_file' dict with 'file_id' and "
                    "'download_path'."
                ),
            },
            "output_filename": {
                "type": "string",
                "description": (
                    "Custom filename for the output CSV (e.g. 'result.csv'). "
                    "Defaults to 'query_result.csv'. Only used when "
                    "output_file=true."
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
        output_file_requested: bool = bool(params.get("output_file") or False)
        output_filename: str = str(params.get("output_filename") or "query_result.csv").strip()
        if not output_filename.lower().endswith(".csv"):
            output_filename += ".csv"
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
            return self._failure(access_error_code(exc), str(exc))
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

        total_rows = len(raw_rows)

        # ── Decide whether to auto-persist the full result ─────────────────
        # If the caller did not explicitly request output_file but the
        # result is larger than the inline preview can hold, we save it
        # automatically and surface a notice to the agent.
        auto_output_file = (
            not output_file_requested and total_rows > _MAX_INLINE_ROWS
        )
        persist_output = output_file_requested or auto_output_file

        # Build the inline preview (capped to _MAX_INLINE_ROWS upfront so the
        # payload trim loop only handles the byte budget afterwards).
        inline_raw_rows = raw_rows[:_MAX_INLINE_ROWS]
        rows_dicts = [dict(zip(columns, r)) for r in inline_raw_rows]
        preview_trimmed_rows = total_rows > len(inline_raw_rows)

        # ── Optional: save full result as a CSV file in MinIO ──────────────
        generated_file: Optional[dict] = None
        if persist_output:
            try:
                # Build a Polars frame from the DuckDB result and serialise
                # via the shared helper (same path as csv_edit / csv_soc).
                result_df = pl.DataFrame(
                    {col: [r[i] for r in raw_rows] for i, col in enumerate(columns)},
                    strict=False,
                )
                csv_bytes = await asyncio.to_thread(df_to_csv_bytes, result_df)

                from src.shared.database import _get_session_maker  # noqa: PLC0415

                async with _get_session_maker()() as db_session:
                    generated_file = await self._store_file(
                        data=csv_bytes,
                        filename=output_filename,
                        content_type="text/csv",
                        agent_context=agent_context,
                        session=db_session,
                        description=(
                            f"csv_query result from '{file_meta.get('filename')}' "
                            f"({result_df.height} rows, {result_df.width} cols)"
                        ),
                    )
                if generated_file is None:
                    logger.warning(
                        "csv_query: _store_file returned None for output '%s'",
                        output_filename,
                    )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("csv_query: could not persist output file: %s", exc)
                # Non-fatal: still return the inline result.

        # Build inline payload, trim rows further if it busts the byte budget.
        import json

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
            "file": {
                "file_id": file_meta.get("file_id"),
                "filename": file_meta.get("filename"),
                "rows": file_meta.get("rows"),
                "columns": file_meta.get("columns"),
            },
            "sql": sql,
            "result": result_payload,
        }
        if generated_file is not None:
            result_data["generated_file"] = generated_file
            if auto_output_file:
                result_data["notice"] = (
                    f"Query result has {total_rows} rows, exceeding the inline "
                    f"preview limit of {_MAX_INLINE_ROWS}. The full result was "
                    "saved automatically as a CSV file — see 'generated_file' "
                    "for the download path / file_id."
                )
        elif preview_trimmed_rows or truncated_rows:
            result_data["notice"] = (
                f"Inline preview shows {len(result_payload['rows'])} of "
                f"{total_rows} rows. Re-run with output_file=true to receive "
                "the full result as a downloadable CSV."
            )
        return self._success(result_data, execution_time_ms=elapsed)


# Re-export to keep import-time cost low — payload helper is only used in
# tests / future expansion.
__all__ = ["CsvQueryTool", "result_to_payload"]
