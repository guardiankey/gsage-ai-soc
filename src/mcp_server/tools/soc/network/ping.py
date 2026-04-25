"""gSage AI — Ping tool stub (Phase 4)."""

from __future__ import annotations

from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext


class PingTool(BaseTool):
    """
    Ping — ICMP echo-request to check if a host is reachable.

    STUB: not yet implemented — external privilege requirements (CAP_NET_RAW)
    need security review before enabling in production.

    Permission: ``network:ping``
    """

    name: ClassVar[str] = "ping"
    version: ClassVar[str] = "0.1.0"
    summary: ClassVar[str] = "ICMP echo-request to check if a host is reachable (stub — not yet implemented)"
    category: ClassVar[str] = "network"
    available: ClassVar[bool] = False  # stub — requires CAP_NET_RAW (security review pending)
    permissions: ClassVar[list[str]] = ["network:ping"]
    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 10
    use_circuit_breaker: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {"target_entities": "target"}

    params_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "required": ["target"],
        "properties": {
            "target": {
                "type": "string",
                "description": "IP address or hostname to ping.",
            },
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "default": 3,
                "description": "Number of ICMP packets to send (1–5, default: 3).",
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "max_packets": {"type": "integer", "description": "Number of ICMP packets (1-5)"},
        "packet_timeout_ms": {"type": "integer", "description": "Per-packet timeout in ms"},
    }
    config_defaults: ClassVar[dict] = {"max_packets": 3, "packet_timeout_ms": 1000}
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
            target (str, required): IP address or hostname to ping.
            count (int, optional): Number of packets to send (1–5, default: 3).
        """
        raise NotImplementedError(
            "PingTool is not yet implemented. "
            "ICMP operations require CAP_NET_RAW capabilities which need "
            "dedicated security review before container deployment."
        )
