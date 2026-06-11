"""gSage AI — Tool Discovery meta-tool.

Allows the LLM to search for available tools by keyword query or category
without receiving the full tool list at conversation start.

This tool reduces token usage by keeping list_tools limited to core tools only.
The LLM calls search_tools whenever it needs a specialized capability not
already in its core list.

Permission: none required — visible to any authenticated user.
"""

from __future__ import annotations

import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

# Available categories (informational — used in param description)
_KNOWN_CATEGORIES = (
    "dns", "network", "email", "threat_intel", "file", "document",
    "itsm", "edr", "kb", "crud", "firewall", "security", "utility",
)


class SearchToolsTool(BaseTool):
    """
    Discover available tools by keyword query or category.

    Use this tool when you need a capability that is not already in your
    core tool set.  Search results include only tools you are authorized
    to use — the list is automatically filtered by your permissions.

    Examples
    --------
    - ``{"query": "block IP firewall"}``         — find firewall/block tools
    - ``{"query": "ticket ITSM GLPI"}``           — find ITSM tools
    - ``{"category": "edr"}``                     — list all EDR tools
    - ``{"query": "email phishing"}``             — find email-analysis tools
    - ``{"show_all": true}``                      — list every available tool
    """

    name: ClassVar[str] = "search_tools"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Search for available tools by keyword or category to discover specialized capabilities"
    category: ClassVar[str] = "utility"
    core_tool: ClassVar[bool] = True

    # No permissions required — this is a meta-tool visible to all authenticated users.
    # Permission isolation is enforced internally via registry.get_tools(agent_context).
    permissions: ClassVar[list[str]] = []
    use_circuit_breaker: ClassVar[bool] = False
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 10

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Free-text search query.  BM25-ranked against tool names, summaries "
                    "and categories.  Examples: 'scan port', 'block IP firewall', "
                    "'GLPI ticket', 'base64 decode'."
                ),
            },
            "category": {
                "type": "string",
                "description": (
                    "Filter by category.  One of: "
                    + ", ".join(f"'{c}'" for c in _KNOWN_CATEGORIES)
                    + ".  Can be combined with 'query'."
                ),
            },
            "show_all": {
                "type": "boolean",
                "description": (
                    "When true, return every tool you are authorized to use without "
                    "applying search or category filters.  Defaults to false."
                ),
                "default": False,
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
        start = time.monotonic()

        query: str = (params.get("query") or "").strip()
        category_filter: str = (params.get("category") or "").strip().lower()
        show_all: bool = bool(params.get("show_all", False))

        # Import registry late to avoid circular imports at module load time.
        from src.mcp_server.registry.registry import get_registry  # noqa: PLC0415

        registry = get_registry()
        # All tools the caller is authorized to use (permission-filtered).
        visible: list[BaseTool] = registry.get_tools(agent_context)

        # Exclude search_tools itself from results to avoid confusion.
        tools = [t for t in visible if t.name != self.name]

        # ── Category filter ─────────────────────────────────────────────────
        if category_filter:
            tools = [
                t for t in tools
                if getattr(t, "category", "general").lower() == category_filter
            ]

        # ── BM25 keyword search ──────────────────────────────────────────────
        if not show_all and query:
            try:
                from rank_bm25 import BM25Okapi  # noqa: PLC0415
            except ImportError:
                # Graceful degradation: fall back to simple substring match.
                q_lower = query.lower()
                tools = [
                    t for t in tools
                    if q_lower in t.name.lower()
                    or q_lower in getattr(t, "summary", "").lower()
                    or q_lower in getattr(t, "category", "").lower()
                ]
            else:
                corpus = [
                    " ".join([
                        t.name.replace("_", " "),
                        getattr(t, "summary", ""),
                        getattr(t, "category", ""),
                    ])
                    for t in tools
                ]
                tokenized = [doc.lower().split() for doc in corpus]
                if tokenized:
                    bm25 = BM25Okapi(tokenized)
                    scores = bm25.get_scores(query.lower().replace("_", " ").split())
                    top_k = 12
                    indexed = sorted(
                        enumerate(scores), key=lambda x: x[1], reverse=True
                    )
                    tools = [
                        tools[i]
                        for i, score in indexed[:top_k]
                        if score > 0
                    ]

        elapsed_ms = int((time.monotonic() - start) * 1000)

        results = [
            {
                "name": t.name,
                "summary": (
                    getattr(t, "summary", None)
                    or (t.__class__.__doc__ or "").strip().splitlines()[0]
                ),
                "category": getattr(t, "category", "general"),
                "requires_config": t.requires_config,
                "requires_approval": t.requires_approval,
                "requires_user_credentials": getattr(
                    t, "requires_user_credentials", False
                ),
                "credential_schema": getattr(t, "credential_schema", None),
                "credential_namespace": getattr(t, "credential_namespace", None),
                "params_schema": t.effective_params_schema,
            }
            for t in tools
        ]

        return self._success(
            data={
                "tools": results,
                "count": len(results),
                "filters_applied": {
                    "query": query or None,
                    "category": category_filter or None,
                    "show_all": show_all,
                },
            },
            execution_time_ms=elapsed_ms,
        )
