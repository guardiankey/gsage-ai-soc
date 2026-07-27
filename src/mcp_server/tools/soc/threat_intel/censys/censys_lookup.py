"""gSage AI — Censys lookup tool (read-only threat intelligence).

Single ``censys_lookup`` MCP tool with actions:

- ``host_view``   — full host detail for an IP (services, ASN, geo, OS).
- ``host_search`` — search the hosts dataset with a Censys query.
- ``host_names``  — DNS names associated with an IP.
- ``aggregate``   — bucketed counts for a query over a chosen field (report).

Authentication: Censys API ID + Secret (per-org, encrypted).
Multi-instance: one GSageToolConfig row per Censys account.

Permission: ``threat:intel``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.result_export import (
    AGENT_PREVIEW_ROWS,
    build_agent_payload,
    summarize,
)
from src.mcp_server.tools.soc.threat_intel.censys._client import (
    CensysClient,
    CensysError,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_CONFIG_SCHEMA: dict = {
    "type": "object",
    "required": ["api_id", "api_secret"],
    "properties": {
        "api_id": {
            "type": "string",
            "description": "Censys API ID (HTTP Basic username).",
            "sensitive": True,
        },
        "api_secret": {
            "type": "string",
            "description": "Censys API Secret (HTTP Basic password).",
            "sensitive": True,
        },
        "base_url": {
            "type": "string",
            "description": (
                "Override the Censys API base URL. Defaults to "
                "https://search.censys.io/api/v2. Set this only if your "
                "account is on a different Censys host."
            ),
        },
        "timeout": {
            "type": "integer",
            "minimum": 5,
            "maximum": 120,
            "description": "HTTP request timeout in seconds (default: 20).",
        },
    },
    "additionalProperties": False,
}


class CensysLookupTool(BaseTool):
    """Read-only Censys lookups for attack-surface and IOC enrichment.

    Actions
    -------
    - ``host_view`` — full Censys record for an IP (requires ``ip``): observed
      services/ports, software, ASN, geolocation, OS and last-seen time.
    - ``host_search`` — Censys Search-language query over the hosts dataset
      (requires ``query``; e.g. ``services.service_name: HTTP and location.country: Brazil``).
      Returns a row per matched host. Supports cursor-based paging via
      ``cursor`` and ``per_page``.
    - ``host_names`` — forward DNS names associated with an IP (requires ``ip``).
    - ``aggregate`` — bucketed counts for a ``query`` grouped by ``field``
      (e.g. ``field=services.port``); returns the top buckets (report view).

    ``host_search`` supports ``export_csv`` / ``export_json`` / ``group_by`` /
    ``top_n`` and caps the inline preview at 100 rows.

    Permission: ``threat:intel``
    """

    name: ClassVar[str] = "censys_lookup"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Censys lookups: host detail for an IP, hosts search (Censys query "
        "language), DNS names and field aggregation"
    )
    category: ClassVar[str] = "threat_intel"
    core_tool: ClassVar[bool] = False
    permissions: ClassVar[list[str]] = ["threat:intel"]

    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 25
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    config_schema: ClassVar[Optional[dict]] = _CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = {"timeout": 20}

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {"target_entities": "ip"}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["host_view", "host_search", "host_names", "aggregate"],
                "description": "Which Censys operation to perform.",
            },
            "ip": {
                "type": "string",
                "description": "Target IP address (host_view, host_names).",
            },
            "query": {
                "type": "string",
                "description": "Censys Search query (host_search, aggregate).",
            },
            "field": {
                "type": "string",
                "description": "Field to aggregate on, e.g. 'services.port' (aggregate).",
            },
            "num_buckets": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "default": 50,
                "description": "Number of aggregation buckets to return (aggregate).",
            },
            "per_page": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 50,
                "description": "Results per page for host_search (max 100).",
            },
            "cursor": {
                "type": "string",
                "description": "Pagination cursor returned by a previous host_search.",
            },
            "export_csv": {
                "type": "boolean",
                "default": False,
                "description": "Persist full search results as a CSV artifact.",
            },
            "export_json": {
                "type": "boolean",
                "default": False,
                "description": "Persist full search results as a JSON artifact.",
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Columns to summarise (top-N) for search results.",
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
                "description": "Top-N limit for the summary block.",
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
        action = params.get("action")
        if not isinstance(action, str) or not action:
            return self._failure("INVALID_INPUT", "'action' is required")

        start = time.monotonic()
        api_id = str(config.get("api_id", ""))
        api_secret = str(config.get("api_secret", ""))
        if not api_id or not api_secret:
            return self._failure(
                "CONFIG_MISSING", "Censys API ID/Secret are not configured."
            )

        base_url = config.get("base_url") or "https://search.censys.io/api/v2"
        try:
            client = CensysClient(
                api_id=api_id,
                api_secret=api_secret,
                timeout=int(config.get("timeout", 20)),
                base_url=str(base_url),
            )
        except CensysError as exc:
            return self._failure("INVALID_CONFIG", str(exc))

        try:
            async with client:
                if action == "host_view":
                    return await self._host_view(params, client, start)
                if action == "host_search":
                    return await self._host_search(agent_context, params, client, start)
                if action == "host_names":
                    return await self._host_names(params, client, start)
                if action == "aggregate":
                    return await self._aggregate(params, client, start)
                return self._failure("INVALID_INPUT", f"Unknown action: {action!r}")
        except CensysError as exc:
            code = "CENSYS_AUTH_ERROR" if exc.status_code in (401, 403) else "CENSYS_API_ERROR"
            if exc.status_code == 404:
                code = "CENSYS_NOT_FOUND"
            return self._failure(code, str(exc), retryable=exc.retryable)

    # ── Actions ────────────────────────────────────────────────────────────

    async def _host_view(self, params, client, start) -> ToolResult:
        ip = params.get("ip")
        if not isinstance(ip, str) or not ip.strip():
            return self._failure("INVALID_INPUT", "'ip' is required for host_view")
        data = await client.get(f"/hosts/{ip.strip()}")
        host = data.get("host", data) if isinstance(data, dict) else data
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success({"action": "host_view", "host": host}, execution_time_ms=elapsed)

    async def _host_search(self, agent_context, params, client, start) -> ToolResult:
        query = params.get("query")
        if not isinstance(query, str) or not query.strip():
            return self._failure("INVALID_INPUT", "'query' is required for host_search")
        q: dict = {"q": query.strip(), "per_page": int(params.get("per_page", 50))}
        if params.get("cursor"):
            q["cursor"] = params["cursor"]
        data = await client.get("/hosts/search", params=q)
        hits = data.get("hits") or []
        rows = [
            {
                "ip": h.get("ip"),
                "asn": (h.get("autonomous_system") or {}).get("asn"),
                "as_name": (h.get("autonomous_system") or {}).get("name"),
                "country": (h.get("location") or {}).get("country"),
                "city": (h.get("location") or {}).get("city"),
                "ports": ", ".join(
                    str(s.get("port")) for s in (h.get("services") or []) if isinstance(s, dict)
                ),
                "service_names": ", ".join(
                    str(s.get("service_name"))
                    for s in (h.get("services") or [])
                    if isinstance(s, dict)
                ),
            }
            for h in hits
            if isinstance(h, dict)
        ]
        links = data.get("links") or {}
        server_total = (data.get("total") if isinstance(data.get("total"), int) else len(rows))
        summary = summarize(
            rows,
            group_by=params.get("group_by") or None,
            top_n=int(params.get("top_n", 10) or 10),
            default_keys=("country", "as_name", "service_names"),
        )
        payload = await build_agent_payload(
            tool=self,
            rows=rows,
            export_csv=bool(params.get("export_csv", False)),
            export_json=bool(params.get("export_json", False)),
            filename_prefix=f"{self.name}_search",
            agent_context=agent_context,
        )
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {
                "action": "host_search",
                "query": query.strip(),
                "server_total_items": server_total,
                "next_cursor": links.get("next") or None,
                "rows_total": payload["rows_total"],
                "rows_overflow": payload["rows_overflow"],
                "rows_preview_limit": AGENT_PREVIEW_ROWS,
                "artifacts": payload["artifacts"],
                "agent_hint": payload["agent_hint"],
                "summary": summary,
                "rows": payload["rows_preview"],
            },
            execution_time_ms=elapsed,
        )

    async def _host_names(self, params, client, start) -> ToolResult:
        ip = params.get("ip")
        if not isinstance(ip, str) or not ip.strip():
            return self._failure("INVALID_INPUT", "'ip' is required for host_names")
        data = await client.get(f"/hosts/{ip.strip()}/names")
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {
                "action": "host_names",
                "ip": ip.strip(),
                "names": data.get("names", data) if isinstance(data, dict) else data,
            },
            execution_time_ms=elapsed,
        )

    async def _aggregate(self, params, client, start) -> ToolResult:
        query = params.get("query")
        field = params.get("field")
        if not isinstance(query, str) or not query.strip():
            return self._failure("INVALID_INPUT", "'query' is required for aggregate")
        if not isinstance(field, str) or not field.strip():
            return self._failure("INVALID_INPUT", "'field' is required for aggregate")
        data = await client.get(
            "/hosts/aggregate",
            params={
                "q": query.strip(),
                "field": field.strip(),
                "num_buckets": int(params.get("num_buckets", 50)),
            },
        )
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {
                "action": "aggregate",
                "query": query.strip(),
                "field": field.strip(),
                "total": data.get("total") if isinstance(data, dict) else None,
                "buckets": data.get("buckets") if isinstance(data, dict) else data,
            },
            execution_time_ms=elapsed,
        )
