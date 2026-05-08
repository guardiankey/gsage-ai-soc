"""gSage AI — Trellix EDR shared tool helpers.

Thin Trellix-specific facade over the generic
:mod:`src.mcp_server.tools.result_export` helpers. Existing call sites
(``from src.mcp_server.tools.soc.edr.trellix._artifacts import
maybe_export, build_agent_payload``) keep working unchanged.

For new tools, prefer importing
:func:`src.mcp_server.tools.result_export.build_agent_payload` directly.
"""

from __future__ import annotations

import logging

from src.mcp_server.tools.base import BaseTool
from src.mcp_server.tools.result_export import (
    build_agent_payload as _build_agent_payload,
)
from src.mcp_server.tools.result_export import (
    maybe_export_artifacts as _maybe_export_artifacts,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


async def maybe_export(
    tool: BaseTool,
    *,
    rows: list[dict],
    export_csv: bool,
    export_json: bool,
    filename_prefix: str,
    agent_context: AgentContext,
) -> dict:
    """Backwards-compatible wrapper around
    :func:`result_export.maybe_export_artifacts`."""
    return await _maybe_export_artifacts(
        tool,
        rows=rows,
        export_csv=export_csv,
        export_json=export_json,
        filename_prefix=filename_prefix,
        agent_context=agent_context,
    )


async def build_agent_payload(
    tool: BaseTool,
    *,
    rows: list[dict],
    export_csv: bool,
    export_json: bool,
    filename_prefix: str,
    agent_context: AgentContext,
) -> dict:
    """Backwards-compatible wrapper around
    :func:`result_export.build_agent_payload`."""
    return await _build_agent_payload(
        tool,
        rows=rows,
        export_csv=export_csv,
        export_json=export_json,
        filename_prefix=filename_prefix,
        agent_context=agent_context,
    )
