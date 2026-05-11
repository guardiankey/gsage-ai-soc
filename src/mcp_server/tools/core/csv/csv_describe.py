"""csv_describe — schema, head, sample, value_counts on stored CSV files.

Reads a CSV from `GSageFile` storage (MinIO) via the shared loader and
returns lightweight descriptive views suitable for an LLM to inspect
without running SQL.

Permission: ``core:csv_describe``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

import polars as pl

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.core.csv.csv_loader import access_error_code, load_csv
from src.shared.security.context import AgentContext

logger = logging.getLogger(__name__)

_MAX_ROWS_HEAD: int = 100
_MAX_ROWS_SAMPLE: int = 100
_MAX_TOP_VALUES: int = 50


def _build_schema_view(df: pl.DataFrame) -> dict:
    """Schema + per-column null/uniqueness summary."""
    height = int(df.height)
    columns: list[dict] = []
    for name, dtype in df.schema.items():
        col = df[name]
        null_count = int(col.null_count())
        try:
            unique_count = int(col.n_unique())
        except Exception:
            unique_count = -1  # n_unique can fail on unhashable nested types
        columns.append({
            "name": name,
            "dtype": str(dtype),
            "null_count": null_count,
            "null_ratio": (null_count / height) if height else 0.0,
            "unique_count": unique_count,
        })
    return {
        "row_count": height,
        "column_count": int(df.width),
        "columns": columns,
    }


def _build_head_view(df: pl.DataFrame, n: int) -> dict:
    n = max(1, min(n, _MAX_ROWS_HEAD))
    return {
        "n": n,
        "rows": df.head(n).to_dicts(),
        "columns": list(df.columns),
    }


def _build_sample_view(df: pl.DataFrame, n: int, seed: Optional[int]) -> dict:
    n = max(1, min(n, _MAX_ROWS_SAMPLE))
    height = int(df.height)
    if height == 0:
        sample_df = df
    elif height <= n:
        sample_df = df
    else:
        sample_df = df.sample(n=n, seed=seed, shuffle=True)
    return {
        "n": int(sample_df.height),
        "rows": sample_df.to_dicts(),
        "columns": list(df.columns),
    }


def _build_value_counts(df: pl.DataFrame, column: str, top: int) -> dict:
    if column not in df.columns:
        raise ValueError(f"Column {column!r} not found.")
    top = max(1, min(top, _MAX_TOP_VALUES))
    counts = (
        df.group_by(column)
        .agg(pl.len().alias("count"))
        .sort("count", descending=True)
        .head(top)
    )
    rows = counts.to_dicts()
    distinct = int(df[column].n_unique())
    return {
        "column": column,
        "distinct_count": distinct,
        "top": rows,
        "truncated": distinct > top,
    }


class CsvDescribeTool(BaseTool):
    """Describe a CSV file: schema, head, sample, or per-column value counts.

    Reads the file referenced by ``file_id`` (a ``GSageFile`` UUID) from the
    organisation's storage, parses it once via the shared CSV loader and
    returns a lightweight view chosen by ``action``.

    Available actions:

    * ``schema``        — column names, dtypes, null counts, unique counts.
    * ``head``          — first N rows (default 10, max 100).
    * ``sample``        — random sample of N rows (default 10, max 100).
    * ``value_counts``  — top values for a single column (default 20,
                          max 50).

    The CSV is parsed with auto-detected encoding and delimiter; both can be
    overridden via ``delimiter`` and ``encoding`` parameters.

    Permission: ``core:csv_describe``
    """

    name: ClassVar[str] = "csv_describe"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Describe a stored CSV file: schema, head rows, random sample, "
        "or value counts per column."
    )
    category: ClassVar[str] = "data"
    permissions: ClassVar[list[str]] = ["core:csv_describe"]
    rate_limit_per_minute: ClassVar[int] = 600
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = False  # local-only, no external deps

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action", "file_id"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["schema", "head", "sample", "value_counts"],
                "description": (
                    "Which descriptive view to return:\n"
                    "- schema: per-column dtype, null count, unique count.\n"
                    "- head: first N rows in file order.\n"
                    "- sample: random N rows.\n"
                    "- value_counts: top values + frequency for one column."
                ),
            },
            "file_id": {
                "type": "string",
                "description": "UUID of the CSV file (GSageFile.id).",
            },
            "n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": (
                    "Number of rows to return for action=head/sample. "
                    "Default 10. Ignored otherwise."
                ),
            },
            "column": {
                "type": "string",
                "description": (
                    "Column name. Required when action=value_counts."
                ),
            },
            "top": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": (
                    "Number of top values to return for action=value_counts. "
                    "Default 20."
                ),
            },
            "seed": {
                "type": "integer",
                "description": "Random seed for action=sample (reproducible).",
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
                    "Override encoding detection (e.g. 'utf-8', 'latin-1'). "
                    "Omit for auto-detect."
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
        if not isinstance(action, str) or action not in {
            "schema", "head", "sample", "value_counts"
        }:
            return self._failure("INVALID_INPUT", "'action' is required.")
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
            logger.exception("csv_describe: unexpected load failure: %s", exc)
            return self._failure(
                "INTERNAL_ERROR",
                f"Failed to load CSV: {exc}",
                retryable=True,
            )

        try:
            if action == "schema":
                view = _build_schema_view(df)
            elif action == "head":
                n = params.get("n", 10)
                if not isinstance(n, int):
                    return self._failure("INVALID_INPUT", "'n' must be an integer.")
                view = _build_head_view(df, n)
            elif action == "sample":
                n = params.get("n", 10)
                seed = params.get("seed")
                if not isinstance(n, int):
                    return self._failure("INVALID_INPUT", "'n' must be an integer.")
                if seed is not None and not isinstance(seed, int):
                    return self._failure("INVALID_INPUT", "'seed' must be an integer.")
                view = _build_sample_view(df, n, seed)
            else:  # value_counts
                column = params.get("column")
                if not isinstance(column, str) or not column:
                    return self._failure(
                        "INVALID_INPUT",
                        "'column' is required for action=value_counts.",
                    )
                top = params.get("top", 20)
                if not isinstance(top, int):
                    return self._failure("INVALID_INPUT", "'top' must be an integer.")
                try:
                    view = _build_value_counts(df, column, top)
                except ValueError as exc:
                    return self._failure("INVALID_INPUT", str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("csv_describe: failed to build view: %s", exc)
            return self._failure("INTERNAL_ERROR", f"Failed to describe CSV: {exc}")

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {
                "action": action,
                "file": {
                    "file_id": file_meta.get("file_id"),
                    "filename": file_meta.get("filename"),
                    "size_bytes": file_meta.get("size_bytes"),
                    "encoding": file_meta.get("encoding"),
                    "delimiter": file_meta.get("delimiter"),
                    "rows": file_meta.get("rows"),
                    "columns": file_meta.get("columns"),
                    "truncated": file_meta.get("truncated", False),
                },
                "result": view,
            },
            execution_time_ms=elapsed,
        )
