"""gSage AI — SentinelOne endpoint (agent) inventory tool (read-only).

Read-only views over SentinelOne agents and their scoping objects:

- ``list_agents``      — Agents with filters (free-text ``query``,
                        ``computer_name``, ``os_type``, ``infected``,
                        ``isolated``, ``site_ids``).
- ``get_agent``        — One agent by ``agent_id`` or ``computer_name``.
- ``agent_activities`` — Recent activity log for one agent.
- ``list_groups``      — Endpoint groups.
- ``list_sites``       — Sites (tenancy / licensing scope).

Tabular results auto-export as CSV over 100 rows. Permission:
``sentinelone:read``. Multiple consoles via per-profile config.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.edr.sentinelone._client import (
    S1_CONFIG_DEFAULTS,
    S1_CONFIG_SCHEMA,
    SentinelOneClient,
    SentinelOneError,
    build_s1_client,
)
from src.mcp_server.tools.soc.edr.sentinelone import _views as V
from src.mcp_server.tools.result_export import (
    AGENT_PREVIEW_ROWS,
    build_agent_payload,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = frozenset({
    "list_agents",
    "get_agent",
    "agent_activities",
    "list_groups",
    "list_sites",
})

_DEFAULT_RESULTS = 100
_MAX_RESULTS = 1000


class _ParamError(Exception):
    pass


def _require(params: dict, field: str) -> str:
    val = params.get(field)
    if isinstance(val, str):
        val = val.strip()
    if val in (None, ""):
        raise _ParamError(f"'{field}' is required for this action.")
    return str(val)


async def _resolve_agent_id(client: SentinelOneClient, params: dict) -> str:
    """Resolve an agent UUID from ``agent_id`` or ``computer_name``."""
    aid = (params.get("agent_id") or "").strip()
    if aid:
        return aid
    name = (params.get("computer_name") or "").strip()
    if not name:
        raise _ParamError("This action requires 'agent_id' or 'computer_name'.")
    rows = await client.paginate(
        "/agents", {"computerName__contains": name}, limit=10, max_items=10
    )
    if not rows:
        raise SentinelOneError(f"No agent matched {name!r}.", code="NOT_FOUND")
    if len(rows) > 1:
        names = ", ".join(str(r.get("computerName")) for r in rows[:5])
        raise SentinelOneError(
            f"computer_name {name!r} matched multiple agents ({names}); "
            "use agent_id.",
            code="CONFLICT",
        )
    return str(rows[0].get("id"))


class S1EndpointsTool(BaseTool):
    """Read-only SentinelOne agent inventory.

    Use one ``action`` per call. Tabular results auto-export as CSV when
    over 100 rows. Permission: ``sentinelone:read``.
    """

    name: ClassVar[str] = "s1_endpoints"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Read-only SentinelOne agents: list/get endpoints, agent "
        "activities, groups, sites. Auto-CSV on >100 rows."
    )
    category: ClassVar[str] = "edr"
    config_namespace: ClassVar[str] = "sentinelone"
    permissions: ClassVar[list[str]] = ["sentinelone:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "target_entities": "computer_name",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which read-only query to run.",
            },
            "profile": {
                "type": "string",
                "description": (
                    "GSageToolConfig profile (S1 console) to use. Omit for "
                    "the 'default' profile."
                ),
            },
            "agent_id": {
                "type": "string",
                "description": (
                    "[get_agent, agent_activities] Agent UUID. Alternative "
                    "to computer_name."
                ),
            },
            "computer_name": {
                "type": "string",
                "description": (
                    "[get_agent] Resolve agent by hostname (must be unique). "
                    "[list_agents] Filter agents whose name contains this."
                ),
            },
            "query": {
                "type": "string",
                "description": "[list_agents] Free-text search across agents.",
            },
            "os_type": {
                "type": "string",
                "enum": ["windows", "linux", "macos"],
                "description": "[list_agents] Filter by OS type.",
            },
            "infected": {
                "type": "boolean",
                "description": "[list_agents] Only agents with active threats.",
            },
            "isolated": {
                "type": "boolean",
                "description": (
                    "[list_agents] Only network-isolated (disconnected) "
                    "agents."
                ),
            },
            "site_ids": {
                "type": "string",
                "description": (
                    "Comma-separated site IDs to scope the query. Defaults "
                    "to the profile's default_site_ids."
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
            "export_csv": {
                "type": "boolean",
                "description": (
                    "Force CSV artifact even for small results (auto over "
                    f"{AGENT_PREVIEW_ROWS} rows)."
                ),
            },
            "export_json": {
                "type": "boolean",
                "description": "Persist the full result as a JSON artifact.",
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = S1_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = S1_CONFIG_DEFAULTS
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
            async with build_s1_client(config) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(client, params, agent_context, max_results)
        except _ParamError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INVALID_PARAMS", str(exc), execution_time_ms=elapsed)
        except SentinelOneError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.code in ("CONNECTION_ERROR", "TIMEOUT", "RATE_LIMITED")
            return self._failure(
                exc.code, str(exc), retryable=retryable, execution_time_ms=elapsed
            )
        except Exception as exc:
            log.exception("s1_endpoints(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(data={"action": action, **data}, execution_time_ms=elapsed)

    # ── Tabular helper ──────────────────────────────────────────────────────

    async def _tabular(
        self, agent_context: AgentContext, action: str, rows: list[dict], params: dict,
    ) -> dict:
        payload = await build_agent_payload(
            tool=self,
            rows=rows,
            export_csv=bool(params.get("export_csv", False)),
            export_json=bool(params.get("export_json", False)),
            filename_prefix=f"{self.name}_{action}",
            agent_context=agent_context,
        )
        return {
            "rows_total": payload["rows_total"],
            "rows_overflow": payload["rows_overflow"],
            "rows_preview_limit": AGENT_PREVIEW_ROWS,
            "artifacts": payload["artifacts"],
            "agent_hint": payload["agent_hint"],
            "rows": payload["rows_preview"],
        }

    # ── Actions ──────────────────────────────────────────────────────────────

    async def _do_list_agents(
        self, client: SentinelOneClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        q: dict = {}
        if (query := (params.get("query") or "").strip()):
            q["query"] = query
        if (name := (params.get("computer_name") or "").strip()):
            q["computerName__contains"] = name
        if (os_type := (params.get("os_type") or "").strip()):
            q["osTypes"] = os_type
        if params.get("infected") is not None:
            q["infected"] = "true" if params.get("infected") else "false"
        if params.get("isolated"):
            q["networkStatuses"] = "disconnected"
        if (sites := client.resolve_site_ids(params)):
            q["siteIds"] = sites
        rows = await client.paginate("/agents", q, max_items=max_results)
        return await self._tabular(
            agent_context, "list_agents", [V.slim_agent(a) for a in rows], params
        )

    async def _do_get_agent(
        self, client: SentinelOneClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        aid = await _resolve_agent_id(client, params)
        body = await client.get("/agents", {"ids": aid})
        row = V.first_or_none(body)
        if row is None:
            raise SentinelOneError(f"Agent {aid} not found.", code="NOT_FOUND")
        return {"agent": V.slim_agent(row)}

    async def _do_agent_activities(
        self, client: SentinelOneClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        aid = await _resolve_agent_id(client, params)
        rows = await client.paginate(
            "/activities", {"agentIds": aid}, max_items=max_results
        )
        return await self._tabular(
            agent_context, "agent_activities",
            [V.slim_activity(a) for a in rows], params,
        )

    async def _do_list_groups(
        self, client: SentinelOneClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        q: dict = {}
        if (sites := client.resolve_site_ids(params)):
            q["siteIds"] = sites
        rows = await client.paginate("/groups", q, max_items=max_results)
        return await self._tabular(
            agent_context, "list_groups", [V.slim_group(g) for g in rows], params
        )

    async def _do_list_sites(
        self, client: SentinelOneClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        body = await client.get("/sites", {"limit": max_results})
        data = body.get("data") or {}
        # /sites nests the list under data.sites
        sites = data.get("sites") if isinstance(data, dict) else data
        rows = sites if isinstance(sites, list) else []
        return await self._tabular(
            agent_context, "list_sites", [V.slim_site(s) for s in rows], params
        )


_ = Any
