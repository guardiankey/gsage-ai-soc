"""MCP tools package exposing shared tool infrastructure and exports.

Tool classes are auto-discovered by ``build_registry()`` from sub-packages
(e.g. ``core/``, ``network/``).  Only infrastructure symbols are exported here.
"""

from src.mcp_server.tools.base import BaseTool, ToolResult

__all__ = [
    "BaseTool",
    "ToolResult",
]
