"""gSage AI — Port Check tool stub (Phase 4)."""

from __future__ import annotations

from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

# Privileged ports are blocked by default (0-1023)
BLOCKED_PORT_RANGE = (0, 1023)
MAX_BATCH_PORTS = 20


class PortCheckTool(BaseTool):
    """
    Port Check — TCP connect scan on a target host/port.

    Checks whether a TCP port is open by attempting a non-blocking connect.
    Privileged ports (0–1023) are blocked.

    STUB: not yet implemented — network scanning requires dedicated SOC role
    authorisation and legal review before deployment.

    Permission: ``network:scan``
    """

    name: ClassVar[str] = "port_check"
    available: ClassVar[bool] = False  # stub — Phase 4 implementation pending
    version: ClassVar[str] = "0.1.0"
    summary: ClassVar[str] = "TCP connect scan to check if a specific port is open on a target host"
    category: ClassVar[str] = "network"
    permissions: ClassVar[list[str]] = ["network:scan"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 15
    use_circuit_breaker: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {"target_entities": "target"}

    params_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "required": ["target", "ports"],
        "properties": {
            "target": {
                "type": "string",
                "description": "IP address or hostname to scan.",
            },
            "ports": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1024, "maximum": 65535},
                "minItems": 1,
                "maxItems": 20,
                "description": (
                    "List of TCP port numbers to check (max 20). "
                    "Privileged ports (0–1023) are blocked unless explicitly authorised."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "connect_timeout_ms": {"type": "integer", "description": "TCP connect timeout (ms)"},
        "allow_privileged_ports": {
            "type": "boolean",
            "description": "Override privileged port block (requires network:scan:privileged permission)",
        },
    }
    config_defaults: ClassVar[dict] = {
        "connect_timeout_ms": 2000,
        "allow_privileged_ports": False,
    }
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
        """
        Params:
            target (str, required): IP address or hostname.
            ports (list[int], required): Port numbers to check (max 20,
                privileged ports 0–1023 blocked unless authorised).
        """
        raise NotImplementedError(
            "PortCheckTool is not yet implemented. "
            "TCP scanning requires legal/authorisation review and dedicated "
            "rate-limiting infrastructure before container deployment."
        )
