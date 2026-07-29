"""gSage AI — Wazuh operational response tool (requires human approval).

Provides a single ``wazuh_manage`` MCP tool that performs **write / response**
actions against the Wazuh Manager API:

- ``active_response`` — run a configured active-response command (e.g.
  ``firewall-drop``, ``host-deny``) on one or all agents.
- ``restart_agent``   — restart the Wazuh agent daemon on a host.
- ``add_to_group``    — assign an agent to a group.
- ``remove_from_group`` — remove an agent from a group.

Every invocation is approval-gated (``requires_approval=True``): the gSage
agent layer collects a human-readable ``_approval_summary`` and waits for
approval before dispatching — the same HITL contract used by ``block_ip``.

Authentication: Wazuh API user/password (JWT under the hood).
Multi-instance: one GSageToolConfig row per Wazuh manager.

Permission: ``wazuh:write``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.monitoring.wazuh._shared import (
    WAZUH_CONFIG_DEFAULTS,
    WAZUH_CONFIG_SCHEMA,
    WazuhError,
    build_client,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


class WazuhManageTool(BaseTool):
    """Approval-gated response/write tool for the Wazuh Manager API.

    Actions
    -------
    - ``active_response`` — trigger an active-response command on agents.
      Requires ``command`` (e.g. ``firewall-drop``). Target either a specific
      ``agent_id`` or all agents via ``all_agents=true``. Optional
      ``arguments`` (list of strings) and ``alert`` (dict) are forwarded to the
      active-response script. Use this to, e.g., null-route a malicious IP on
      an endpoint.
    - ``restart_agent`` — restart the Wazuh agent on the host (``agent_id``).
    - ``add_to_group`` — add ``agent_id`` to ``group``.
    - ``remove_from_group`` — remove ``agent_id`` from ``group``.

    Safety
    ------
    ``requires_approval = True`` — the agent must collect an ``_approval_summary``
    and obtain human approval before this tool runs. The tool itself remains
    safe if called directly (all parameters are validated).

    Permission: ``wazuh:write``
    """

    name: ClassVar[str] = "wazuh_manage"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Wazuh response actions (active-response, restart agent, group "
        "membership) — requires human approval"
    )
    category: ClassVar[str] = "monitoring"
    core_tool: ClassVar[bool] = False
    permissions: ClassVar[list[str]] = ["wazuh:write"]

    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    config_schema: ClassVar[Optional[dict]] = WAZUH_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = WAZUH_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {"target_entities": "agent_id"}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "active_response",
                    "restart_agent",
                    "add_to_group",
                    "remove_from_group",
                ],
                "description": "Which response/write operation to perform.",
            },
            "agent_id": {
                "type": "string",
                "description": (
                    "Target agent ID (e.g. '001'). Required for restart_agent, "
                    "add_to_group, remove_from_group, and for active_response "
                    "unless 'all_agents' is true."
                ),
            },
            "all_agents": {
                "type": "boolean",
                "default": False,
                "description": "For active_response: apply to all agents instead of one.",
            },
            "command": {
                "type": "string",
                "description": (
                    "Active-response command name as configured on the manager "
                    "(e.g. 'firewall-drop', 'host-deny', 'restart-wazuh'). "
                    "Required for active_response. A leading '!' is added "
                    "automatically if omitted."
                ),
            },
            "arguments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional argument list passed to the active-response script.",
            },
            "alert": {
                "type": "object",
                "description": (
                    "Optional alert payload forwarded to the active-response "
                    "script (e.g. {'data': {'srcip': '1.2.3.4'}})."
                ),
                "additionalProperties": True,
            },
            "group": {
                "type": "string",
                "description": "Group name for add_to_group / remove_from_group.",
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
                if action == "active_response":
                    return await self._active_response(params, client, start)
                if action == "restart_agent":
                    return await self._restart_agent(params, client, start)
                if action == "add_to_group":
                    return await self._group_membership(params, client, start, remove=False)
                if action == "remove_from_group":
                    return await self._group_membership(params, client, start, remove=True)
                return self._failure("INVALID_INPUT", f"Unknown action: {action!r}")
        except WazuhError as exc:
            code = "WAZUH_API_ERROR"
            if exc.status_code in (401, 403):
                code = "WAZUH_AUTH_ERROR"
            return self._failure(code, str(exc), retryable=exc.retryable)

    # ── Actions ────────────────────────────────────────────────────────────

    async def _active_response(self, params, client, start) -> ToolResult:
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            return self._failure("INVALID_INPUT", "'command' is required for active_response")
        command = command.strip()
        if not command.startswith("!"):
            command = f"!{command}"

        all_agents = bool(params.get("all_agents", False))
        agent_id = params.get("agent_id")
        if not all_agents and (not isinstance(agent_id, str) or not agent_id.strip()):
            return self._failure(
                "INVALID_INPUT",
                "Provide 'agent_id' or set 'all_agents=true' for active_response",
            )

        body: dict = {"command": command}
        if params.get("arguments"):
            body["arguments"] = list(params["arguments"])
        if isinstance(params.get("alert"), dict):
            body["alert"] = params["alert"]

        query: dict = {}
        if all_agents:
            query["agents_list"] = "all"
        else:
            query["agents_list"] = agent_id.strip()

        data = await client.request(
            "PUT", "/active-response", params=query, json_body=body
        )
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {
                "action": "active_response",
                "command": command,
                "target": "all" if all_agents else agent_id.strip(),
                "result": data,
            },
            execution_time_ms=elapsed,
        )

    async def _restart_agent(self, params, client, start) -> ToolResult:
        agent_id = params.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return self._failure("INVALID_INPUT", "'agent_id' is required for restart_agent")
        data = await client.request(
            "PUT", "/agents/restart", params={"agents_list": agent_id.strip()}
        )
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {"action": "restart_agent", "agent_id": agent_id.strip(), "result": data},
            execution_time_ms=elapsed,
        )

    async def _group_membership(self, params, client, start, *, remove: bool) -> ToolResult:
        agent_id = params.get("agent_id")
        group = params.get("group")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return self._failure("INVALID_INPUT", "'agent_id' is required")
        if not isinstance(group, str) or not group.strip():
            return self._failure("INVALID_INPUT", "'group' is required")
        method = "DELETE" if remove else "PUT"
        action = "remove_from_group" if remove else "add_to_group"
        data = await client.request(
            method, f"/agents/{agent_id.strip()}/group/{group.strip()}"
        )
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            {
                "action": action,
                "agent_id": agent_id.strip(),
                "group": group.strip(),
                "result": data,
            },
            execution_time_ms=elapsed,
        )
