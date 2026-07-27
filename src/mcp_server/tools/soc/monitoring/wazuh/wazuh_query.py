"""gSage AI — Wazuh read-only query tool.

Provides a single ``wazuh_query`` MCP tool with actions covering:

- Inventory : agents_list, agent_details, agents_summary, groups_list
- Health    : manager_status, manager_info
- Ruleset   : rules_list
- Assessment: sca_results (Security Configuration Assessment),
              fim_results (File Integrity Monitoring / syscheck)

Authentication: Wazuh API user/password (JWT under the hood).
Multi-instance: one GSageToolConfig row per Wazuh manager
(profile_id = "prod", "client-a", etc.).

Permission: ``wazuh:read``
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
from src.mcp_server.tools.soc.monitoring.wazuh._shared import (
    WAZUH_CONFIG_DEFAULTS,
    WAZUH_CONFIG_SCHEMA,
    WazuhError,
    build_client,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# Actions that return a tabular row set → routed through result_export.
_LIST_ACTIONS = {
    "agents_list",
    "groups_list",
    "rules_list",
    "sca_results",
    "fim_results",
}


class WazuhQueryTool(BaseTool):
    """Read-only query tool for the Wazuh Manager API.

    **Inventory**
    - ``agents_list`` — list/search agents (filter by ``status``, ``group``,
      ``search`` name/IP). Returns id, name, ip, os, status, version, group.
    - ``agent_details`` — full record for one agent (requires ``agent_id``).
    - ``agents_summary`` — counts of agents by connection status
      (active / disconnected / pending / never_connected).
    - ``groups_list`` — configured agent groups and their member counts.

    **Manager health**
    - ``manager_status`` — running state of each Wazuh daemon.
    - ``manager_info`` — manager version, compilation and node info.

    **Ruleset**
    - ``rules_list`` — installed detection rules (filter by ``level``,
      ``group``, ``search``).

    **Assessment**
    - ``sca_results`` — Security Configuration Assessment policy results for
      one agent (requires ``agent_id``): pass/fail counts per policy.
    - ``fim_results`` — File Integrity Monitoring (syscheck) findings for one
      agent (requires ``agent_id``); filter by ``search`` on file path.

    List-style actions support ``export_csv`` / ``export_json`` / ``group_by``
    / ``top_n`` and cap the inline preview at 100 rows (full set exported as a
    downloadable artifact).

    Permission: ``wazuh:read``
    """

    name: ClassVar[str] = "wazuh_query"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Query Wazuh: agent inventory & status, manager health, detection "
        "rules, SCA and File Integrity Monitoring results"
    )
    category: ClassVar[str] = "monitoring"
    core_tool: ClassVar[bool] = False
    permissions: ClassVar[list[str]] = ["wazuh:read"]

    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    config_schema: ClassVar[Optional[dict]] = WAZUH_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = WAZUH_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {"target_entities": "agent_id"}
    audit_output: ClassVar[bool] = False  # responses can be large

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "agents_list",
                    "agent_details",
                    "agents_summary",
                    "groups_list",
                    "manager_status",
                    "manager_info",
                    "rules_list",
                    "sca_results",
                    "fim_results",
                ],
                "description": "Which read operation to perform.",
            },
            "agent_id": {
                "type": "string",
                "description": (
                    "Agent ID (e.g. '001'). Required for 'agent_details', "
                    "'sca_results' and 'fim_results'."
                ),
            },
            "status": {
                "type": "string",
                "enum": ["active", "disconnected", "pending", "never_connected"],
                "description": "Filter agents by connection status (agents_list).",
            },
            "group": {
                "type": "string",
                "description": "Filter agents (agents_list) or rules (rules_list) by group.",
            },
            "level": {
                "type": "integer",
                "minimum": 0,
                "maximum": 16,
                "description": "Minimum rule level filter (rules_list).",
            },
            "search": {
                "type": "string",
                "description": "Free-text search filter (name/IP, rule text, file path).",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "default": 100,
                "description": "Maximum number of rows to fetch (default 100).",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Pagination offset (default 0).",
            },
            "export_csv": {
                "type": "boolean",
                "default": False,
                "description": "Persist the full result set as a CSV artifact.",
            },
            "export_json": {
                "type": "boolean",
                "default": False,
                "description": "Persist the full result set as a JSON artifact.",
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Columns to summarise (top-N counts) for list actions.",
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
        try:
            client = build_client(config)
        except WazuhError as exc:
            return self._failure("INVALID_CONFIG", str(exc), retryable=exc.retryable)

        try:
            async with client:
                if action == "agents_list":
                    return await self._agents_list(agent_context, params, client)
                if action == "agent_details":
                    return await self._agent_details(params, client, start)
                if action == "agents_summary":
                    return await self._agents_summary(client, start)
                if action == "groups_list":
                    return await self._groups_list(agent_context, params, client)
                if action == "manager_status":
                    return await self._manager_status(client, start)
                if action == "manager_info":
                    return await self._manager_info(client, start)
                if action == "rules_list":
                    return await self._rules_list(agent_context, params, client)
                if action == "sca_results":
                    return await self._sca_results(agent_context, params, client)
                if action == "fim_results":
                    return await self._fim_results(agent_context, params, client)
                return self._failure("INVALID_INPUT", f"Unknown action: {action!r}")
        except WazuhError as exc:
            code = "WAZUH_API_ERROR"
            if exc.status_code in (401, 403):
                code = "WAZUH_AUTH_ERROR"
            return self._failure(code, str(exc), retryable=exc.retryable)

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _rows(data: object) -> list[dict]:
        """Extract the ``affected_items`` row list from a Wazuh data envelope."""
        if isinstance(data, dict):
            items = data.get("affected_items")
            if isinstance(items, list):
                return [r for r in items if isinstance(r, dict)]
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        return []

    @staticmethod
    def _total(data: object, fallback: int) -> int:
        if isinstance(data, dict) and isinstance(data.get("total_affected_items"), int):
            return data["total_affected_items"]
        return fallback

    async def _tabular(
        self,
        agent_context: AgentContext,
        params: dict,
        action: str,
        rows: list[dict],
        server_total: int,
        default_keys: tuple[str, ...],
        start: float,
    ) -> ToolResult:
        """Shape a row set into the standard result_export payload."""
        summary = summarize(
            rows,
            group_by=params.get("group_by") or None,
            top_n=int(params.get("top_n", 10) or 10),
            default_keys=default_keys,
        )
        payload = await build_agent_payload(
            tool=self,
            rows=rows,
            export_csv=bool(params.get("export_csv", False)),
            export_json=bool(params.get("export_json", False)),
            filename_prefix=f"{self.name}_{action}",
            agent_context=agent_context,
        )
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {
                "action": action,
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

    # ── Actions ────────────────────────────────────────────────────────────

    async def _agents_list(self, agent_context, params, client) -> ToolResult:
        start = time.monotonic()
        query: dict = {
            "limit": int(params.get("limit", 100)),
            "offset": int(params.get("offset", 0)),
        }
        if params.get("status"):
            query["status"] = params["status"]
        if params.get("group"):
            query["group"] = params["group"]
        if params.get("search"):
            query["search"] = params["search"]
        query["select"] = "id,name,ip,os.name,os.version,status,version,group,lastKeepAlive,node_name"
        data = await client.request("GET", "/agents", params=query)
        rows = [self._flatten_agent(a) for a in self._rows(data)]
        return await self._tabular(
            agent_context, params, "agents_list", rows,
            self._total(data, len(rows)),
            ("status", "group", "os_name", "version"), start,
        )

    @staticmethod
    def _flatten_agent(agent: dict) -> dict:
        """Flatten the nested ``os`` object and join the group list for CSV."""
        os_obj = agent.get("os") or {}
        group = agent.get("group")
        return {
            "id": agent.get("id"),
            "name": agent.get("name"),
            "ip": agent.get("ip"),
            "status": agent.get("status"),
            "os_name": os_obj.get("name") if isinstance(os_obj, dict) else None,
            "os_version": os_obj.get("version") if isinstance(os_obj, dict) else None,
            "version": agent.get("version"),
            "group": ", ".join(group) if isinstance(group, list) else group,
            "last_keep_alive": agent.get("lastKeepAlive"),
            "node_name": agent.get("node_name"),
        }

    async def _agent_details(self, params, client, start) -> ToolResult:
        agent_id = params.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return self._failure("INVALID_INPUT", "'agent_id' is required for agent_details")
        data = await client.request(
            "GET", "/agents", params={"agents_list": agent_id.strip()}
        )
        rows = self._rows(data)
        if not rows:
            return self._failure("NOT_FOUND", f"Agent {agent_id!r} not found")
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success({"action": "agent_details", "agent": rows[0]}, execution_time_ms=elapsed)

    async def _agents_summary(self, client, start) -> ToolResult:
        data = await client.request("GET", "/agents/summary/status")
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success({"action": "agents_summary", "summary": data}, execution_time_ms=elapsed)

    async def _groups_list(self, agent_context, params, client) -> ToolResult:
        start = time.monotonic()
        query = {
            "limit": int(params.get("limit", 100)),
            "offset": int(params.get("offset", 0)),
        }
        if params.get("search"):
            query["search"] = params["search"]
        data = await client.request("GET", "/groups", params=query)
        rows = self._rows(data)
        return await self._tabular(
            agent_context, params, "groups_list", rows,
            self._total(data, len(rows)), ("name",), start,
        )

    async def _manager_status(self, client, start) -> ToolResult:
        data = await client.request("GET", "/manager/status")
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success({"action": "manager_status", "daemons": data}, execution_time_ms=elapsed)

    async def _manager_info(self, client, start) -> ToolResult:
        data = await client.request("GET", "/manager/info")
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success({"action": "manager_info", "info": data}, execution_time_ms=elapsed)

    async def _rules_list(self, agent_context, params, client) -> ToolResult:
        start = time.monotonic()
        query: dict = {
            "limit": int(params.get("limit", 100)),
            "offset": int(params.get("offset", 0)),
        }
        if params.get("level") is not None:
            # Wazuh accepts a level range like "7-16".
            query["level"] = f"{int(params['level'])}-16"
        if params.get("group"):
            query["group"] = params["group"]
        if params.get("search"):
            query["search"] = params["search"]
        data = await client.request("GET", "/rules", params=query)
        rows = self._rows(data)
        return await self._tabular(
            agent_context, params, "rules_list", rows,
            self._total(data, len(rows)),
            ("level", "groups", "gdpr", "pci_dss"), start,
        )

    async def _sca_results(self, agent_context, params, client) -> ToolResult:
        start = time.monotonic()
        agent_id = params.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return self._failure("INVALID_INPUT", "'agent_id' is required for sca_results")
        query = {
            "limit": int(params.get("limit", 100)),
            "offset": int(params.get("offset", 0)),
        }
        data = await client.request(
            "GET", f"/sca/{agent_id.strip()}", params=query
        )
        rows = self._rows(data)
        return await self._tabular(
            agent_context, params, "sca_results", rows,
            self._total(data, len(rows)),
            ("policy_id", "pass", "fail", "score"), start,
        )

    async def _fim_results(self, agent_context, params, client) -> ToolResult:
        start = time.monotonic()
        agent_id = params.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return self._failure("INVALID_INPUT", "'agent_id' is required for fim_results")
        query: dict = {
            "limit": int(params.get("limit", 100)),
            "offset": int(params.get("offset", 0)),
        }
        if params.get("search"):
            query["search"] = params["search"]
        data = await client.request(
            "GET", f"/syscheck/{agent_id.strip()}", params=query
        )
        rows = self._rows(data)
        return await self._tabular(
            agent_context, params, "fim_results", rows,
            self._total(data, len(rows)),
            ("type", "file", "date"), start,
        )
