"""gSage AI — OPNsense read-only firewall tool.

Read-only triage views over an OPNsense firewall via its REST API.
Covers the queries a SOC analyst needs while investigating or before a
response action.

- ``list_aliases``         — Firewall aliases (name, type, entry counts).
- ``get_alias_entries``     — Live entries of one alias (e.g. the blocklist).
- ``list_rules``            — Filter rules (action, interface, src/dst, …).
- ``firewall_log``          — Recent firewall log lines (pass/block), with
                            an optional ``ip`` substring filter.
- ``query_states``          — Live state table (active connections),
                            filterable by ``ip``.
- ``ids_alerts``            — Suricata IDS/IPS alerts (optional ``ip``).
- ``ids_status``            — Suricata service status.
- ``gateway_status``        — Gateway health (status / loss / latency).
- ``dhcp_leases``           — DHCPv4 leases (address / MAC / hostname).
- ``arp_table``             — ARP table.
- ``list_services``         — OPNsense service states.

Tabular results auto-export as CSV over 100 rows. Permission:
``firewall:read``. Multiple firewalls via per-profile config.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.firewall.opnsense._cache import (
    CACHE_TTL_SECONDS,
    build_cache_key,
    cache_get,
    cache_set,
)
from src.mcp_server.tools.soc.firewall.opnsense._client import (
    OPNSENSE_CONFIG_DEFAULTS,
    OPNSENSE_CONFIG_SCHEMA,
    OPNsenseClient,
    OPNsenseError,
    build_opnsense_client,
)
from src.mcp_server.tools.soc.firewall.opnsense import _views as V
from src.mcp_server.tools.result_export import (
    AGENT_PREVIEW_ROWS,
    build_agent_payload,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = frozenset({
    "list_aliases",
    "get_alias_entries",
    "list_rules",
    "firewall_log",
    "query_states",
    "ids_alerts",
    "ids_status",
    "gateway_status",
    "dhcp_leases",
    "arp_table",
    "list_services",
})

_DEFAULT_RESULTS = 100
_MAX_RESULTS = 2000


class _ParamError(Exception):
    pass


def _rows(resp: Any) -> list[dict]:
    """Extract a grid/list payload from an OPNsense response."""
    if isinstance(resp, dict):
        for key in ("rows", "items", "data"):
            val = resp.get(key)
            if isinstance(val, list):
                return val
        return []
    if isinstance(resp, list):
        return resp
    return []


class OPNsenseFirewallTool(BaseTool):
    """Read-only OPNsense firewall triage.

    Use one ``action`` per call. Tabular results auto-export as CSV when
    over 100 rows. Permission: ``firewall:read``.
    """

    name: ClassVar[str] = "opnsense_firewall"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Read-only OPNsense firewall: aliases, rules, firewall log, live "
        "states, Suricata IDS alerts, gateways, DHCP leases, ARP, services. "
        "Auto-CSV on >100 rows."
    )
    category: ClassVar[str] = "firewall"
    config_namespace: ClassVar[str] = "opnsense"
    permissions: ClassVar[list[str]] = ["firewall:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "target_entities": "ip",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which read-only firewall query to run.",
            },
            "profile": {
                "type": "string",
                "description": (
                    "GSageToolConfig profile (firewall) to use. Omit for "
                    "the 'default' profile."
                ),
            },
            "alias": {
                "type": "string",
                "description": (
                    "[get_alias_entries] Alias name. Defaults to the "
                    "profile's configured block_alias."
                ),
            },
            "ip": {
                "type": "string",
                "description": (
                    "[firewall_log, query_states, ids_alerts] Filter results "
                    "to those mentioning this IP (substring match)."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_RESULTS,
                "description": (
                    f"Maximum items to return (default {_DEFAULT_RESULTS}, "
                    f"hard cap {_MAX_RESULTS})."
                ),
            },
            "force_refresh": {
                "type": "boolean",
                "description": (
                    "Bypass the Redis cache for cacheable reads (alias / "
                    f"rule / service lists). Cache TTL is {CACHE_TTL_SECONDS}s."
                ),
            },
            "export_csv": {
                "type": "boolean",
                "description": (
                    "Force CSV artifact even for small results. CSV is "
                    f"generated automatically over {AGENT_PREVIEW_ROWS} rows."
                ),
            },
            "export_json": {
                "type": "boolean",
                "description": "Persist the full result as a JSON artifact.",
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = OPNSENSE_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = OPNSENSE_CONFIG_DEFAULTS
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Execute ─────────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        action = (params.get("action") or "").strip()
        if action not in _ACTIONS:
            return self._failure(
                "INVALID_PARAMS",
                f"action must be one of {sorted(_ACTIONS)}; got {action!r}.",
            )
        max_results = min(
            int(params.get("max_results") or _DEFAULT_RESULTS), _MAX_RESULTS
        )

        try:
            async with build_opnsense_client(config) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(
                    client, params, agent_context, max_results
                )
        except _ParamError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INVALID_PARAMS", str(exc), execution_time_ms=elapsed
            )
        except OPNsenseError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.code in ("CONNECTION_ERROR", "TIMEOUT", "RATE_LIMITED")
            return self._failure(
                exc.code, str(exc), retryable=retryable,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("opnsense_firewall(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={"action": action, **data}, execution_time_ms=elapsed
        )

    # ── Tabular + cache helpers ──────────────────────────────────────────────

    async def _tabular(
        self,
        agent_context: AgentContext,
        action: str,
        rows: list[dict],
        params: dict,
        *,
        cache_hit: bool = False,
    ) -> dict:
        agent_payload = await build_agent_payload(
            tool=self,
            rows=rows,
            export_csv=bool(params.get("export_csv", False)),
            export_json=bool(params.get("export_json", False)),
            filename_prefix=f"{self.name}_{action}",
            agent_context=agent_context,
        )
        return {
            "rows_total": agent_payload["rows_total"],
            "rows_overflow": agent_payload["rows_overflow"],
            "rows_preview_limit": AGENT_PREVIEW_ROWS,
            "artifacts": agent_payload["artifacts"],
            "agent_hint": agent_payload["agent_hint"],
            "rows": agent_payload["rows_preview"],
            "cache_hit": cache_hit,
        }

    async def _cached_list(
        self,
        agent_context: AgentContext,
        client: OPNsenseClient,
        action: str,
        params: dict,
        filters: dict,
        rows_fn: Any,
        max_results: int,
    ) -> dict:
        org = str(getattr(agent_context, "org_id", "") or "")
        user = str(getattr(agent_context, "user_id", "") or "")
        profile = str(params.get("profile") or "default")
        key = build_cache_key(
            org_id=org, user_id=user, profile_id=profile,
            fw_host=client.host, kind=action, filters=filters,
        )
        if not params.get("force_refresh"):
            cached = await cache_get(key)
            if isinstance(cached, list):
                return await self._tabular(
                    agent_context, action, cached[:max_results], params,
                    cache_hit=True,
                )
        rows = await rows_fn()
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, action, rows[:max_results], params
        )

    @staticmethod
    def _ip_filter(rows: list[dict], ip: str) -> list[dict]:
        if not ip:
            return rows
        needle = ip.strip()
        return [
            r for r in rows
            if any(needle in str(v) for v in r.values() if v is not None)
        ]

    # ── Cacheable list actions ───────────────────────────────────────────────

    async def _do_list_aliases(
        self, client: OPNsenseClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        async def _rows_fn() -> list[dict]:
            resp = await client.post(
                "/firewall/alias/searchItem",
                {"current": 1, "rowCount": _MAX_RESULTS},
            )
            return [V.slim_alias(r) for r in _rows(resp)]
        return await self._cached_list(
            agent_context, client, "list_aliases", params, {}, _rows_fn,
            max_results,
        )

    async def _do_list_rules(
        self, client: OPNsenseClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        async def _rows_fn() -> list[dict]:
            resp = await client.post(
                "/firewall/filter/searchRule",
                {"current": 1, "rowCount": _MAX_RESULTS},
            )
            return [V.slim_rule(r) for r in _rows(resp)]
        return await self._cached_list(
            agent_context, client, "list_rules", params, {}, _rows_fn,
            max_results,
        )

    async def _do_list_services(
        self, client: OPNsenseClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        async def _rows_fn() -> list[dict]:
            resp = await client.post(
                "/core/service/search", {"current": 1, "rowCount": _MAX_RESULTS}
            )
            return _rows(resp)
        return await self._cached_list(
            agent_context, client, "list_services", params, {}, _rows_fn,
            max_results,
        )

    async def _do_get_alias_entries(
        self, client: OPNsenseClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        alias = (params.get("alias") or "").strip() or client.block_alias
        if not alias:
            raise _ParamError(
                "alias is required (pass 'alias' or set 'block_alias' in the "
                "profile)."
            )
        resp = await client.get(f"/firewall/alias_util/list/{alias}")
        entries = _rows(resp)
        return {
            "alias": alias,
            "entry_count": len(entries),
            "entries": entries[:max_results],
        }

    # ── Live (uncached) actions ──────────────────────────────────────────────

    async def _do_firewall_log(
        self, client: OPNsenseClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        resp = await client.get(
            "/diagnostics/firewall/log", limit=max_results
        )
        rows = [V.slim_log_entry(r) for r in _rows(resp)]
        rows = self._ip_filter(rows, params.get("ip") or "")
        return await self._tabular(
            agent_context, "firewall_log", rows[:max_results], params
        )

    async def _do_query_states(
        self, client: OPNsenseClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        ip = (params.get("ip") or "").strip()
        resp = await client.post(
            "/diagnostics/firewall/queryStates",
            {"current": 1, "rowCount": max_results, "searchPhrase": ip},
        )
        rows = [V.slim_state(r) for r in _rows(resp)]
        return await self._tabular(
            agent_context, "query_states", rows[:max_results], params
        )

    async def _do_ids_alerts(
        self, client: OPNsenseClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        ip = (params.get("ip") or "").strip()
        resp = await client.post(
            "/ids/service/queryAlerts",
            {"current": 1, "rowCount": max_results, "searchPhrase": ip},
        )
        rows = [V.slim_ids_alert(r) for r in _rows(resp)]
        return await self._tabular(
            agent_context, "ids_alerts", rows[:max_results], params
        )

    async def _do_ids_status(
        self, client: OPNsenseClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        status = await client.get("/ids/service/status")
        return {"status": status}

    async def _do_gateway_status(
        self, client: OPNsenseClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        resp = await client.get("/routes/gateway/status")
        rows = [V.slim_gateway(r) for r in _rows(resp)]
        return await self._tabular(
            agent_context, "gateway_status", rows[:max_results], params
        )

    async def _do_dhcp_leases(
        self, client: OPNsenseClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        resp = await client.get("/dhcpv4/leases/searchLease")
        rows = [V.slim_lease(r) for r in _rows(resp)]
        rows = self._ip_filter(rows, params.get("ip") or "")
        return await self._tabular(
            agent_context, "dhcp_leases", rows[:max_results], params
        )

    async def _do_arp_table(
        self, client: OPNsenseClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        resp = await client.get("/diagnostics/interface/getArp")
        rows = _rows(resp)
        rows = self._ip_filter(rows, params.get("ip") or "")
        return await self._tabular(
            agent_context, "arp_table", rows[:max_results], params
        )


_ = Any
