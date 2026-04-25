"""gSage AI — Knowledge Base CRUD tool.

Allows the AI agent to read and write the organization's knowledge base
during a conversation. Storage backend: Weaviate (semantic search via
text2vec-ollama — no external embedding API required).

    create  — store a new knowledge entry  (requires crud:knowledge_base:write)
    search  — semantic / keyword search    (requires crud:knowledge_base:read)
    list    — list recent active entries   (requires crud:knowledge_base:read)
    delete  — soft-delete an entry         (requires crud:knowledge_base:write)

Feature flags (environment variables):
    CRUD_TOOLS_ENABLED=true         — enables the tool in the registry
    CRUD_TOOLS_ALLOW_WRITE=true     — enables create / delete actions

Tenant isolation: every query is scoped to agent_context.org_id.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import ClassVar, Optional

from src.mcp_server.tools.base import ToolResult
from src.mcp_server.tools.crud_base import CrudBaseTool
from src.shared.models.knowledge_base import GSageKnowledgeSource
from src.shared.security.context import AgentContext
from src.shared.services.knowledge_service import KnowledgeService


class KnowledgeBaseCrudTool(CrudBaseTool):
    """
    CRUD tool for the gSage per-org knowledge base (Weaviate backend).

    Single tool with action dispatch — LLM selects the action via params.
    """

    name: ClassVar[str] = "knowledge_base"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Search, read, create and manage the organization's knowledge base articles (Weaviate backend)"
    category: ClassVar[str] = "kb"
    core_tool: ClassVar[bool] = True
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 15

    # Configure CrudBaseTool dispatch
    valid_actions: ClassVar[frozenset[str]] = frozenset({"create", "search", "list", "delete"})
    write_actions: ClassVar[frozenset[str]] = frozenset({"create", "delete"})
    write_permission: ClassVar[str] = "crud:knowledge_base:write"

    # Traditional permission tags (for registry filtering)
    permissions: ClassVar[list[str]] = ["crud:knowledge_base:read", "crud:knowledge_base:write"]

    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # Describe params so the LLM knows how to call the tool
    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "search", "list", "delete"],
                "description": (
                    "create: save a new knowledge entry. "
                    "search: semantic/keyword search. "
                    "list: list recent active entries. "
                    "delete: soft-delete an entry by ID."
                ),
            },
            "content": {
                "type": "string",
                "description": "[create] Text to remember (required for create).",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "[create] Optional tags for categorization.",
            },
            "user_scoped": {
                "type": "boolean",
                "default": True,
                "description": (
                    "[create/search/list] true = user-level entry; "
                    "false = org-wide entry visible to all users."
                ),
            },
            "expires_at": {
                "type": "string",
                "description": "[create] Optional ISO-8601 expiration date (UTC).",
            },
            "query": {
                "type": "string",
                "description": "[search] Text to search for.",
            },
            "limit": {
                "type": "integer",
                "default": 20,
                "description": "[list] Maximum number of entries to return (max 100).",
            },
            "offset": {
                "type": "integer",
                "default": 0,
                "description": "[list] Pagination offset.",
            },
            "entry_id": {
                "type": "string",
                "description": "[delete] UUID of the entry to soft-delete.",
            },
        },
    }

    # ── Action handlers ──────────────────────────────────────────────────────

    async def _handle_create(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session,
        start: float,
    ) -> ToolResult:
        content = params.get("content", "").strip()
        if not content:
            return self._failure(
                code="INVALID_PARAMS",
                message="Parameter 'content' is required and cannot be empty.",
            )

        tags: list[str] = params.get("tags", [])
        user_scoped: bool = params.get("user_scoped", True)
        expires_at: Optional[datetime] = None

        expires_at_raw = params.get("expires_at")
        if expires_at_raw:
            try:
                expires_at = datetime.fromisoformat(expires_at_raw).replace(tzinfo=timezone.utc)
            except ValueError:
                return self._failure(
                    code="INVALID_PARAMS",
                    message=f"'expires_at' is not a valid ISO-8601 date: {expires_at_raw!r}",
                )

        svc = KnowledgeService()

        try:
            entry = await svc.store_entry(
                content=content,
                agent_context=agent_context,
                source=GSageKnowledgeSource.AGENT_AUTO,
                is_validated=False,
                user_scoped=user_scoped,
                tags=tags,
                expires_at=expires_at,
            )
        except ValueError as exc:
            return self._failure(code="LIMIT_EXCEEDED", message=str(exc))

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={
                "id": entry.id,
                "content": entry.content,
                "version": entry.version,
                "user_scoped": user_scoped,
                "tags": entry.tags,
            },
            execution_time_ms=elapsed,
        )

    async def _handle_search(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session,
        start: float,
    ) -> ToolResult:
        query = params.get("query", "").strip()
        if not query:
            return self._failure(
                code="INVALID_PARAMS",
                message="Parameter 'query' is required.",
            )

        user_scoped: bool = params.get("user_scoped", True)

        svc = KnowledgeService()
        results = await svc.search_similar(query, agent_context, user_scoped=user_scoped)

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"query": query, "results": results, "count": len(results)},
            execution_time_ms=elapsed,
        )

    async def _handle_list(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session,
        start: float,
    ) -> ToolResult:
        user_scoped: bool = params.get("user_scoped", True)
        limit: int = min(int(params.get("limit", 20)), 100)
        offset: int = max(int(params.get("offset", 0)), 0)

        svc = KnowledgeService()
        entries = await svc.list_entries(
            agent_context, user_scoped=user_scoped, limit=limit, offset=offset
        )

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={
                "entries": [
                    {
                        "id": e.id,
                        "content": e.content,
                        "source": e.source,
                        "is_validated": e.is_validated,
                        "tags": e.tags,
                        "user_scoped": e.user_id is not None,
                        "created_at": e.created_at,
                        "expires_at": e.expires_at,
                    }
                    for e in entries
                ],
                "count": len(entries),
                "offset": offset,
                "limit": limit,
            },
            execution_time_ms=elapsed,
        )

    async def _handle_delete(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session,
        start: float,
    ) -> ToolResult:
        entry_id = params.get("entry_id", "").strip()
        if not entry_id:
            return self._failure(
                code="INVALID_PARAMS",
                message="Parameter 'entry_id' is required.",
            )

        svc = KnowledgeService()
        deleted = await svc.delete_entry(entry_id, agent_context)

        if not deleted:
            return self._failure(
                code="NOT_FOUND",
                message=f"Entry '{entry_id}' not found or does not belong to your organization.",
            )

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"deleted_id": entry_id},
            execution_time_ms=elapsed,
        )

