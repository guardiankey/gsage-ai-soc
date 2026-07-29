"""gSage AI — Shodan lookup tool (read-only threat intelligence).

Single ``shodan_lookup`` MCP tool with actions:

- ``host_info``    — banners/services/vulns for an IP (consumes 1 query credit).
- ``search``       — search the Shodan index with a query (1 credit/page).
- ``count``        — result count for a query (free, no results returned).
- ``dns_resolve``  — resolve hostnames → IPs.
- ``dns_reverse``  — reverse-resolve IPs → hostnames.
- ``domain_info``  — subdomains and DNS records for a domain.
- ``account``      — account profile / remaining query credits.

Authentication: Shodan API key (per-org, encrypted).
Multi-instance: one GSageToolConfig row per Shodan account.

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
from src.mcp_server.tools.soc.threat_intel.shodan._client import (
    ShodanClient,
    ShodanError,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_CONFIG_SCHEMA: dict = {
    "type": "object",
    "required": ["api_key"],
    "properties": {
        "api_key": {
            "type": "string",
            "description": "Shodan API key.",
            "sensitive": True,
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


class ShodanLookupTool(BaseTool):
    """Read-only Shodan lookups for attack-surface and IOC enrichment.

    Actions
    -------
    - ``host_info`` — all services, banners, open ports, hostnames, tags and
      known CVEs Shodan has observed for an IP (requires ``ip``; **1 query
      credit**).
    - ``search`` — Shodan search-language query across the whole index
      (requires ``query``; e.g. ``apache country:BR port:443``). Returns a row
      per matched service. **1 query credit per page.**
    - ``count`` — number of results for a ``query`` without returning matches
      (free — good for scoping before a paid search).
    - ``dns_resolve`` — resolve ``hostnames`` (comma-separated) to IPs.
    - ``dns_reverse`` — reverse-resolve ``ips`` (comma-separated) to hostnames.
    - ``domain_info`` — subdomains and DNS records for a ``domain``.
    - ``account`` — account plan and remaining ``query_credits`` /
      ``scan_credits``.

    Permission: ``threat:intel``
    """

    name: ClassVar[str] = "shodan_lookup"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Shodan lookups: host/service banners & CVEs for an IP, index search, "
        "DNS resolve/reverse and domain intel"
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
                "enum": [
                    "host_info",
                    "search",
                    "count",
                    "dns_resolve",
                    "dns_reverse",
                    "domain_info",
                    "account",
                ],
                "description": "Which Shodan operation to perform.",
            },
            "ip": {
                "type": "string",
                "description": "Target IP address (host_info).",
            },
            "query": {
                "type": "string",
                "description": "Shodan search query (search / count).",
            },
            "hostnames": {
                "type": "string",
                "description": "Comma-separated hostnames to resolve (dns_resolve).",
            },
            "ips": {
                "type": "string",
                "description": "Comma-separated IPs to reverse-resolve (dns_reverse).",
            },
            "domain": {
                "type": "string",
                "description": "Domain name (domain_info).",
            },
            "page": {
                "type": "integer",
                "minimum": 1,
                "default": 1,
                "description": "Result page for search (1 credit/page).",
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
        api_key = str(config.get("api_key", ""))
        if not api_key:
            return self._failure("CONFIG_MISSING", "Shodan API key is not configured.")

        try:
            client = ShodanClient(api_key=api_key, timeout=int(config.get("timeout", 20)))
        except ShodanError as exc:
            return self._failure("INVALID_CONFIG", str(exc))

        try:
            async with client:
                if action == "host_info":
                    return await self._host_info(params, client, start)
                if action == "search":
                    return await self._search(agent_context, params, client, start)
                if action == "count":
                    return await self._count(params, client, start)
                if action == "dns_resolve":
                    return await self._dns_resolve(params, client, start)
                if action == "dns_reverse":
                    return await self._dns_reverse(params, client, start)
                if action == "domain_info":
                    return await self._domain_info(params, client, start)
                if action == "account":
                    return await self._account(client, start)
                return self._failure("INVALID_INPUT", f"Unknown action: {action!r}")
        except ShodanError as exc:
            code = "SHODAN_AUTH_ERROR" if exc.status_code in (401, 403) else "SHODAN_API_ERROR"
            if exc.status_code == 404:
                code = "SHODAN_NOT_FOUND"
            return self._failure(code, str(exc), retryable=exc.retryable)

    # ── Actions ────────────────────────────────────────────────────────────

    async def _host_info(self, params, client, start) -> ToolResult:
        ip = params.get("ip")
        if not isinstance(ip, str) or not ip.strip():
            return self._failure("INVALID_INPUT", "'ip' is required for host_info")
        data = await client.get(f"/shodan/host/{ip.strip()}")
        elapsed = int((time.monotonic() - start) * 1000)
        # Trim the (potentially huge) per-service banner blob to essentials.
        services = [
            {
                "port": s.get("port"),
                "transport": s.get("transport"),
                "product": s.get("product"),
                "version": s.get("version"),
                "cpe": s.get("cpe"),
                "module": (s.get("_shodan") or {}).get("module"),
            }
            for s in (data.get("data") or [])
            if isinstance(s, dict)
        ]
        summary = {
            "ip": data.get("ip_str"),
            "org": data.get("org"),
            "isp": data.get("isp"),
            "asn": data.get("asn"),
            "country": data.get("country_name"),
            "city": data.get("city"),
            "os": data.get("os"),
            "hostnames": data.get("hostnames"),
            "domains": data.get("domains"),
            "tags": data.get("tags"),
            "vulns": data.get("vulns"),
            "ports": data.get("ports"),
            "last_update": data.get("last_update"),
        }
        return self._success(
            {"action": "host_info", "host": summary, "services": services},
            execution_time_ms=elapsed,
        )

    async def _search(self, agent_context, params, client, start) -> ToolResult:
        query = params.get("query")
        if not isinstance(query, str) or not query.strip():
            return self._failure("INVALID_INPUT", "'query' is required for search")
        data = await client.get(
            "/shodan/host/search",
            params={"query": query.strip(), "page": int(params.get("page", 1))},
        )
        matches = data.get("matches") or []
        rows = [
            {
                "ip": m.get("ip_str"),
                "port": m.get("port"),
                "transport": m.get("transport"),
                "product": m.get("product"),
                "org": m.get("org"),
                "isp": m.get("isp"),
                "asn": m.get("asn"),
                "country": (m.get("location") or {}).get("country_name"),
                "hostnames": ", ".join(m.get("hostnames") or []),
                "domains": ", ".join(m.get("domains") or []),
                "timestamp": m.get("timestamp"),
            }
            for m in matches
            if isinstance(m, dict)
        ]
        server_total = data.get("total") if isinstance(data.get("total"), int) else len(rows)
        summary = summarize(
            rows,
            group_by=params.get("group_by") or None,
            top_n=int(params.get("top_n", 10) or 10),
            default_keys=("port", "product", "org", "country"),
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
                "action": "search",
                "query": query.strip(),
                "server_total_items": server_total,
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

    async def _count(self, params, client, start) -> ToolResult:
        query = params.get("query")
        if not isinstance(query, str) or not query.strip():
            return self._failure("INVALID_INPUT", "'query' is required for count")
        data = await client.get("/shodan/host/count", params={"query": query.strip()})
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {
                "action": "count",
                "query": query.strip(),
                "total": data.get("total"),
                "facets": data.get("facets"),
            },
            execution_time_ms=elapsed,
        )

    async def _dns_resolve(self, params, client, start) -> ToolResult:
        hostnames = params.get("hostnames")
        if not isinstance(hostnames, str) or not hostnames.strip():
            return self._failure("INVALID_INPUT", "'hostnames' is required for dns_resolve")
        data = await client.get("/dns/resolve", params={"hostnames": hostnames.strip()})
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {"action": "dns_resolve", "resolved": data},
            execution_time_ms=elapsed,
        )

    async def _dns_reverse(self, params, client, start) -> ToolResult:
        ips = params.get("ips")
        if not isinstance(ips, str) or not ips.strip():
            return self._failure("INVALID_INPUT", "'ips' is required for dns_reverse")
        data = await client.get("/dns/reverse", params={"ips": ips.strip()})
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {"action": "dns_reverse", "hostnames": data},
            execution_time_ms=elapsed,
        )

    async def _domain_info(self, params, client, start) -> ToolResult:
        domain = params.get("domain")
        if not isinstance(domain, str) or not domain.strip():
            return self._failure("INVALID_INPUT", "'domain' is required for domain_info")
        data = await client.get(f"/dns/domain/{domain.strip()}")
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {
                "action": "domain_info",
                "domain": domain.strip(),
                "subdomains": data.get("subdomains"),
                "tags": data.get("tags"),
                "records": data.get("data"),
            },
            execution_time_ms=elapsed,
        )

    async def _account(self, client, start) -> ToolResult:
        profile = await client.get("/account/profile")
        info = await client.get("/api-info")
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {"action": "account", "profile": profile, "api_info": info},
            execution_time_ms=elapsed,
        )
