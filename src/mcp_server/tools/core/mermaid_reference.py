"""gSage AI — Mermaid diagram syntax reference tool.

Provides the LLM with accurate, version-specific Mermaid syntax references
to avoid generating incompatible or non-existent diagram types.

This is an internal utility tool — no permissions required.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# Directory containing the curated Mermaid reference files
_REFS_DIR = Path(__file__).parent / "mermaid_refs"

# Map diagram_type aliases to reference file names (without .md)
_DIAGRAM_ALIASES: dict[str, str] = {
    # Canonical names (lowercased, dashes/underscores stripped — see resolve below)
    "flowchart": "flowchart",
    "sequencediagram": "sequence_diagram",
    "classdiagram": "class_diagram",
    "statediagram": "state_diagram",
    "erdiagram": "er_diagram",
    "journey": "journey",
    "gantt": "gantt",
    "pie": "pie",
    "mindmap": "mindmap",
    "timeline": "timeline",
    "zenuml": "zenuml",
    "sankey": "sankey",
    "sankeybeta": "sankey",
    "xychart": "xychart",
    "xychartbeta": "xychart",
    "packet": "packet",
    "packetbeta": "packet",
    "block": "block",
    "blockbeta": "block",
    "kanban": "kanban",
    "architecture": "architecture",
    "architecturebeta": "architecture",
    "radar": "radar",
    "radarbeta": "radar",
    "gitgraph": "gitgraph",
    "quadrantchart": "quadrant_chart",
    "requirementdiagram": "requirement_diagram",
    "c4": "c4",
    "c4context": "c4",
    "c4container": "c4",
    "c4component": "c4",
    "c4dynamic": "c4",
    "c4deployment": "c4",
    # Common short aliases
    "sequence": "sequence_diagram",
    "sequencediagram": "sequence_diagram",
    "class": "class_diagram",
    "state": "state_diagram",
    "statediagramv2": "state_diagram",
    "er": "er_diagram",
    "erd": "er_diagram",
    "git": "gitgraph",
    "quadrant": "quadrant_chart",
    "requirement": "requirement_diagram",
}


def _read_ref(filename: str) -> str | None:
    """Read a reference file from the mermaid_refs directory."""
    path = _REFS_DIR / f"{filename}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


class MermaidReferenceTool(BaseTool):
    """
    Retrieve accurate Mermaid diagram syntax reference.

    Use this tool BEFORE generating a Mermaid diagram to ensure you are
    using the correct keyword, syntax, and avoiding known pitfalls.

    Examples
    --------
    - ``{"diagram_type": "flowchart"}``        — flowchart / graph syntax
    - ``{"diagram_type": "sankey"}``            — sankey-beta syntax and pitfalls
    - ``{"diagram_type": "xychart"}``           — xychart-beta syntax
    - ``{"diagram_type": "gitgraph"}``          — Git graph syntax
    - ``{"diagram_type": "c4"}``                — C4 diagram syntax (all levels)
    - ``{"diagram_type": "quadrantChart"}``     — Quadrant chart syntax
    - ``{"diagram_type": "requirementDiagram"}``— Requirement diagram syntax
    - ``{"diagram_type": "architecture"}``      — architecture-beta (⚠️ dev-only)
    - ``{}``                                    — returns the full index of supported types
    """

    name: ClassVar[str] = "mermaid_reference"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Get Mermaid diagram syntax reference to generate correct diagrams. "
        "Call this before creating any Mermaid diagram to avoid version incompatibilities."
    )
    category: ClassVar[str] = "utility"
    core_tool: ClassVar[bool] = True

    # No permissions required — available to all authenticated users.
    permissions: ClassVar[list[str]] = []
    use_circuit_breaker: ClassVar[bool] = False
    rate_limit_per_minute: ClassVar[int] = 120
    timeout_seconds: ClassVar[int] = 5

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "diagram_type": {
                "type": "string",
                "description": (
                    "The Mermaid diagram type to look up. Examples: 'flowchart', "
                    "'sequenceDiagram', 'classDiagram', 'stateDiagram', 'erDiagram', "
                    "'journey', 'gantt', 'pie', 'mindmap', 'timeline', 'zenuml', "
                    "'sankey' (keyword: sankey-beta), "
                    "'xychart' (keyword: xychart-beta), "
                    "'packet' (keyword: packet-beta), "
                    "'block' (keyword: block-beta), "
                    "'gitgraph', 'c4', 'quadrantChart', 'requirementDiagram', "
                    "'kanban' (dev-only), 'architecture' (dev-only), 'radar' (dev-only). "
                    "Omit to get the full index of supported diagram types."
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: object | None,
        state: object | None,
    ) -> ToolResult:
        diagram_type: str | None = params.get("diagram_type")

        # No diagram_type → return the index
        if not diagram_type:
            content = _read_ref("_index")
            if content is None:
                return self._failure(
                    code="REF_NOT_FOUND",
                    message="Mermaid reference index not found.",
                    retryable=False,
                )
            return self._success(
                data={"reference": content, "diagram_type": "index"},
            )

        # Resolve alias — normalise to lowercase, strip dashes, underscores and spaces
        key = diagram_type.lower().replace("-", "").replace("_", "").replace(" ", "")
        filename = _DIAGRAM_ALIASES.get(key)

        if filename is None:
            # Unknown type — return index with a hint
            index_content = _read_ref("_index") or ""
            return self._failure(
                code="UNKNOWN_DIAGRAM_TYPE",
                message=(
                    f"Unknown diagram type: '{diagram_type}'. "
                    f"See the index for supported types."
                ),
                retryable=False,
            )

        content = _read_ref(filename)
        if content is None:
            return self._failure(
                code="REF_NOT_FOUND",
                message=f"Reference file for '{diagram_type}' not found.",
                retryable=False,
            )

        return self._success(
            data={"reference": content, "diagram_type": diagram_type},
        )
