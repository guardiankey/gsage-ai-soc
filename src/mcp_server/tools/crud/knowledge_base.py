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
from src.shared.services.knowledge_service import KbUserSoftLimitError, KnowledgeService


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
            "kind": {
                "type": "string",
                "enum": ["note", "memory"],
                "default": "note",
                "description": (
                    "[create] 'note' = explicit user request to remember; "
                    "'memory' = persistent personal preference/style "
                    "captured automatically (forces user_scoped=true and "
                    "applies a sensitive-content filter)."
                ),
            },
            "previous_id": {
                "type": "string",
                "description": (
                    "[create] UUID of a prior entry that this one supersedes. "
                    "When supplied, the previous entry is marked inactive and "
                    "the new entry inherits its version+1."
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
        kind: str = (params.get("kind") or "note").strip().lower()
        previous_id_raw = params.get("previous_id")
        previous_id: Optional[str] = (
            previous_id_raw.strip() if isinstance(previous_id_raw, str) and previous_id_raw.strip() else None
        )

        if kind == "memory":
            # USER_MEMORY entries are personal by definition: force scope and
            # mark the source so the auto-injection / sensitivity filter
            # kick in regardless of how the LLM populates ``user_scoped``.
            user_scoped = True
            source_enum = GSageKnowledgeSource.USER_MEMORY
        else:
            source_enum = GSageKnowledgeSource.AGENT_AUTO

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
                source=source_enum,
                is_validated=False,
                user_scoped=user_scoped,
                previous_id=previous_id,
                tags=tags,
                expires_at=expires_at,
            )
        except KbUserSoftLimitError as exc:
            return self._failure(code="USER_MEMORY_SOFT_LIMIT", message=str(exc))
        except ValueError as exc:
            # KnowledgeService raises ValueError for sensitive-content rejection
            # AND for hard limit overflow; both should surface to the LLM.
            msg = str(exc)
            code = "SENSITIVE_CONTENT" if "sensitive" in msg.lower() else "LIMIT_EXCEEDED"
            return self._failure(code=code, message=msg)

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={
                "id": entry.id,
                "content": entry.content,
                "version": entry.version,
                "user_scoped": user_scoped,
                "kind": kind,
                "source": entry.source,
                "superseded_previous_id": previous_id,
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
        entries = await svc.search_entries(query, agent_context, user_scoped=user_scoped)

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={
                "query": query,
                "results": [
                    {
                        "id": entry_id,
                        "content": content,
                        "score": score,
                        "source": src,
                    }
                    for entry_id, content, score, src in entries
                ],
                "count": len(entries),
            },
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

