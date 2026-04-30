"""gSage AI — Trellix EDR generic search tool.

Dispatches to the v2 realtime SQL-like search (``query`` param) or to the
v1 Active Response structured search (``payload`` param).  Always runs in
the background — the search starts on Trellix, the worker polls until the
result is ready (HTTP 303), then fetches all pages, summarises and
optionally exports CSV/JSON.

Permission: ``edr:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.edr.trellix import _query as Q
from src.mcp_server.tools.soc.edr.trellix._artifacts import maybe_export
from src.mcp_server.tools.soc.edr.trellix._client import TrellixEDRError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


class TrellixEdrSearchTool(BaseTool):
    """Run a Trellix EDR search and return summarised results.

    Pass exactly one of:

    - ``query``   — v2 SQL-like (``"HostInfo hostname WHERE HostInfo hostname contains 'foo'"``).
    - ``payload`` — v1 structured Active Response payload (``{"projections": [...], "condition": {...}}``).

    Always background, since searches typically take 30 s – 5 min.

    Output (``data``)::

        query_id, api_version, total_count, total_hosts, truncated,
        summary: { row_count, distinct, top, sample },
        rows: [...up to max_rows...],
        artifacts: { csv_file, json_file }
    """

    name: ClassVar[str] = "trellix_edr_search"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Hunt across endpoints with Trellix EDR realtime searches "
        "(SQL-like v2 or structured v1)"
    )
    category: ClassVar[str] = "edr"
    permissions: ClassVar[list[str]] = ["edr:read"]

    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 900
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    always_background: ClassVar[bool] = True

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    config_schema: ClassVar[Optional[dict]] = Q.TRELLIX_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = Q.TRELLIX_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {"target_entities": "query"}
    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "v2 realtime search expression (SQL-like syntax).  "
                    "Example: \"HostInfo hostname, ip_address WHERE HostInfo hostname contains '00'\".  "
                    "Mutually exclusive with 'payload'."
                ),
            },
            "payload": {
                "type": "object",
                "description": (
                    "v1 Active Response structured payload "
                    "({\"projections\": [...], \"condition\": {...}}).  "
                    "Mutually exclusive with 'query'."
                ),
            },
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": Q.HARD_MAX_ROWS,
                "default": Q.DEFAULT_MAX_ROWS,
                "description": (
                    f"Maximum rows to include in 'rows' (default {Q.DEFAULT_MAX_ROWS}, "
                    f"hard cap {Q.HARD_MAX_ROWS}).  Full result set is always included "
                    "in the CSV/JSON artifact when export is enabled."
                ),
            },
            "export_csv": {
                "type": "boolean",
                "default": False,
                "description": "Persist all rows as a CSV file artifact.",
            },
            "export_json": {
                "type": "boolean",
                "default": False,
                "description": "Persist all rows as a JSON file artifact.",
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of column names to use for top-N analytics "
                    "(overrides the default heuristic).  Use names from the "
                    "flattened result (e.g. 'HostInfo_hostname', 'Files_sha1')."
                ),
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
                "description": "Top-N size for each grouped column (default: 10).",
            },
        },
        "additionalProperties": False,
    }

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        query = params.get("query")
        payload = params.get("payload")

        if (query and payload) or (not query and not payload):
            return self._failure(
                "INVALID_INPUT",
                "Provide exactly one of 'query' (v2) or 'payload' (v1).",
            )

        if query and not isinstance(query, str):
            return self._failure("INVALID_INPUT", "'query' must be a string.")
        if payload and not isinstance(payload, dict):
            return self._failure("INVALID_INPUT", "'payload' must be an object.")

        api_version: Q.ApiVersion = "v2" if query else "v1"
        max_rows = Q.clamp_max_rows(params.get("max_rows"))
        export_csv = bool(params.get("export_csv", False))
        export_json = bool(params.get("export_json", False))
        group_by = params.get("group_by") or None
        top_n = int(params.get("top_n", 10) or 10)

        try:
            async with Q.build_client(config) as client:
                query_id, rows, meta, truncated = await Q.run_search_pipeline(
                    client,
                    api_version=api_version,
                    query=query if isinstance(query, str) else None,
                    payload=payload if isinstance(payload, dict) else None,
                    max_rows=max_rows,
                )
        except TrellixEDRError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.status_code in (429, 500, 502, 503, 504)
            return self._failure(exc.code, str(exc), retryable=retryable, execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("trellix_edr_search: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        summary = Q.summarize(rows, group_by=group_by, top_n=top_n)
        artifacts = await maybe_export(
            self,
            rows=rows,
            export_csv=export_csv,
            export_json=export_json,
            filename_prefix=f"trellix_edr_search_{query_id}",
            agent_context=agent_context,
        )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            {
                "query_id": query_id,
                "api_version": api_version,
                "total_count": meta.get("total_count", len(rows)),
                "total_hosts": meta.get("total_hosts", 0),
                "truncated": truncated,
                "summary": summary,
                "rows": rows,
                "artifacts": artifacts,
            },
            execution_time_ms=elapsed,
        )
