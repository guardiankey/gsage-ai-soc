"""custom_code/tools/example_tool.py — Minimal custom tool example.

Copy this file, rename the class and ``name`` ClassVar, and implement
``execute()``.  The tool will be auto-discovered by the MCP server registry
at startup as long as:
  - It is a concrete (non-abstract) subclass of ``BaseTool``.
  - It has a ``name`` ClassVar.
  - ``available`` is True (the default).

Sub-directory layout example:
    custom_code/
        tools/
            __init__.py          ← required
            network/
                __init__.py      ← required for walk_packages to recurse
                my_network_tool.py
            example_tool.py      ← this file

YAML config defaults (optional):
    Place a file named ``example_tool.yaml`` alongside this module to supply
    default config values.  The class ``config_defaults`` dict always wins on
    key collisions with YAML defaults.

    example_tool.yaml:
        api_url: "https://api.example.com"
        timeout_seconds: 30
"""

from __future__ import annotations

from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext


class ExampleCustomTool(BaseTool):
    """
    Example custom tool — replace with your implementation.

    Permission: ``custom:example``
    """

    name: ClassVar[str] = "example_custom"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Example custom tool — replace with your own implementation"
    category: ClassVar[str] = "utility"
    available: ClassVar[bool] = False  # set to True to activate
    permissions: ClassVar[list[str]] = ["custom:example"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = False

    config_schema: ClassVar[Optional[dict]] = {
        "api_url": {
            "type": "string",
            "description": "Base URL of the external API",
        },
    }
    config_defaults: ClassVar[dict] = {
        "api_url": "https://api.example.com",
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
            input (str, required): The value to process.
        """
        input_value = params.get("input", "")

        # ── Replace the logic below with your real implementation ────────
        result = {"echo": input_value, "tool": self.name}
        # ────────────────────────────────────────────────────────────────

        return ToolResult.success(
            data=result,
            tool_name=self.name,
            version=self.version,
            execution_time_ms=0,
        )
