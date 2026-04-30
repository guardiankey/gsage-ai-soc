"""gSage AI — Block IP Address tool (requires human approval)."""

from __future__ import annotations

from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext


class BlockIPTool(BaseTool):
    """
    Block IP Address — add an IP to the organization's deny-list.

    Adds the specified IP to the firewall deny-list.  Traffic from the IP
    will be dropped for the configured duration (0 = permanent).

    Permission: ``firewall:write``
    """

    name: ClassVar[str] = "block_ip"
    version: ClassVar[str] = "0.1.0"
    summary: ClassVar[str] = "Add an IP address to the organization's firewall deny-list (requires human approval)"
    category: ClassVar[str] = "firewall"
    available: ClassVar[bool] = False  # stub with simulated response for HITL testing
    permissions: ClassVar[list[str]] = ["firewall:write"]
    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 30
    requires_approval: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {"target_entities": "ip"}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "ip": {
                "type": "string",
                "description": "IPv4 or IPv6 address to block.",
            },
            "reason": {
                "type": "string",
                "description": "Justification for blocking the IP.",
            },
            "duration_hours": {
                "type": "integer",
                "description": "Block duration in hours. 0 = permanent.",
                "default": 24,
            },
        },
        "required": ["ip", "reason"],
    }

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        ip = params.get("ip", "")
        if not isinstance(ip, str) or not ip.strip():
            return self._failure("INVALID_INPUT", "'ip' is required")
        reason = params.get("reason", "")
        if not isinstance(reason, str) or not reason.strip():
            return self._failure("INVALID_INPUT", "'reason' is required")

        duration = params.get("duration_hours", 24)

        # Stub: simulated response until firewall integration is implemented
        return self._success({
            "ip": ip.strip(),
            "reason": reason.strip(),
            "duration_hours": duration,
            "status": "blocked",
            "note": "Simulated — firewall integration not yet configured.",
        })
