"""gSage AI — Knowledge Base service (Weaviate semantic search).

Provides CRUD + semantic search operations on the per-org knowledge base
stored in Weaviate.  Vectorization is handled server-side by the
``text2vec-ollama`` module — no Python-side embedding code required.

Location: ``src.shared.services`` because all dependencies live in
``src.shared`` (weaviate_client, models, security context, settings).
This service is consumed by:
    - ``src.mcp_server.tools.crud.knowledge_base``  (MCP CRUD tool)
    - ``src.backend_api``  (agent knowledge base via agno)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from weaviate.classes.query import Filter, MetadataQuery, Sort

from src.shared.config.settings import get_settings
from src.shared.models.knowledge_base import GSageKnowledgeSource
from src.shared.security.context import AgentContext
from src.shared.weaviate_client import COLLECTION_NAME, get_weaviate_client

logger = logging.getLogger(__name__)

# ── Limits ─────────────────────────────────────────────────────────────────
_MAX_ENTRIES_PER_ORG = 10_000
_MAX_ENTRIES_PER_USER = 1_000
_TOP_K_RESULTS = 5
# nomic-embed-text default num_ctx is 2048 tokens (~6 chars/token conservatively → ~5000 chars).
# Truncate to avoid "input length exceeds context length" from Ollama.
_MAX_CONTENT_CHARS = 5_000


# ── Data transfer object ───────────────────────────────────────────────────
@dataclass
class KnowledgeEntry:
    """Lightweight representation of a Weaviate knowledge base object."""

    id: str
    content: str
    org_id: str
    user_id: Optional[str]
    source: str
    is_validated: bool
    version: int
    is_active: bool
    tags: list[str] = field(default_factory=list)
    superseded_by_id: Optional[str] = None
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    dept_id: Optional[str] = None


# ── Service ────────────────────────────────────────────────────────────────
class KnowledgeService:
    """Per-org/user knowledge base operations backed by Weaviate.

    Operations:
        search_similar  — Semantic top-K via nearText (BM25 fallback)
        store_entry     — Append-only insert with automatic vectorization
        list_entries    — Paginated listing with org/user filters
        get_entry       — Fetch single entry by UUID
        delete_entry    — Soft-delete (set is_active=False)
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    # ── Public API ─────────────────────────────────────────────────────────

    async def search_similar(
        self,
        query: str,
        agent_context: AgentContext,
        *,
        user_scoped: bool = True,
    ) -> list[str]:
        """Return top-K content strings via nearText (BM25 fallback)."""
        try:
            client = await get_weaviate_client()
            collection = client.collections.get(COLLECTION_NAME)
            filters = self._build_filters(agent_context, user_scoped)

            try:
                result = await collection.query.near_text(
                    query=query,
                    filters=filters,
                    limit=_TOP_K_RESULTS,
                    return_properties=["content"],
                    return_metadata=MetadataQuery(score=True),
                    target_vector="default",
                )
                return [str(obj.properties["content"]) for obj in result.objects]
            except Exception as exc:
                logger.warning("nearText failed (%s) — BM25 fallback", exc)
                return await self._bm25_search(query, agent_context, user_scoped)

        except Exception as exc:
            logger.warning("Weaviate search failed: %s", exc)
            return []

    async def store_entry(
        self,
        content: str,
        agent_context: AgentContext,
        *,
        source: GSageKnowledgeSource = GSageKnowledgeSource.USER_REQUEST,
        is_validated: bool = False,
        user_scoped: bool = True,
        previous_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
        expires_at: Optional[datetime] = None,
    ) -> KnowledgeEntry:
        """Append-only insert with automatic vectorization.

        Raises:
            ValueError: If org or user limits are exceeded.
        """
        # Truncate content to avoid Ollama "input length exceeds context length" errors.
        # nomic-embed-text typically runs with num_ctx=2048; large markdown easily overflows.
        if len(content) > _MAX_CONTENT_CHARS:
            logger.warning(
                "KB entry content truncated from %d to %d chars (org=%s)",
                len(content), _MAX_CONTENT_CHARS, agent_context.org_id,
            )
            content = content[:_MAX_CONTENT_CHARS]

        client = await get_weaviate_client()
        collection = client.collections.get(COLLECTION_NAME)

        await self._enforce_limits(agent_context, user_scoped)

        # Versioning
        version = 1
        if previous_id:
            try:
                prev_obj = await collection.query.fetch_object_by_id(
                    uuid=previous_id,
                    return_properties=["version"],
                )
                if prev_obj:
                    raw_ver = prev_obj.properties.get("version", 1)
                    version = int(str(raw_ver)) + 1
            except Exception as exc:
                logger.warning("Could not fetch previous entry %s: %s", previous_id, exc)

        now = datetime.now(timezone.utc)
        new_uuid = str(uuid.uuid4())

        properties: dict = {
            "content": content,
            "org_id": str(agent_context.org_id),
            "user_id": str(agent_context.user_id) if user_scoped else "",
            "dept_id": str(agent_context.dept_id) if agent_context.dept_id else "",
            "source": source.value,
            "is_validated": is_validated,
            "version": version,
            "is_active": True,
            "tags": tags or [],
            "superseded_by_id": "",
            "created_at": now,
        }
        if expires_at is not None:
            properties["expires_at"] = expires_at

        await collection.data.insert(properties=properties, uuid=new_uuid)
        logger.debug(
            "Stored KB entry %s (org=%s, version=%d)",
            new_uuid, agent_context.org_id, version,
        )

        # Mark previous entry as superseded
        if previous_id:
            try:
                await collection.data.update(
                    uuid=previous_id,
                    properties={"is_active": False, "superseded_by_id": new_uuid},
                )
            except Exception as exc:
                logger.warning("Could not supersede entry %s: %s", previous_id, exc)

        return KnowledgeEntry(
            id=new_uuid,
            content=content,
            org_id=str(agent_context.org_id),
            user_id=str(agent_context.user_id) if user_scoped else None,
            source=source.value,
            is_validated=is_validated,
            version=version,
            is_active=True,
            tags=tags or [],
            created_at=now.isoformat(),
            expires_at=expires_at.isoformat() if expires_at else None,
            dept_id=str(agent_context.dept_id) if agent_context.dept_id else None,
        )

    async def list_entries(
        self,
        agent_context: AgentContext,
        *,
        user_scoped: bool = True,
        limit: int = 20,
        offset: int = 0,
    ) -> list[KnowledgeEntry]:
        """Paginated listing of active entries."""
        client = await get_weaviate_client()
        collection = client.collections.get(COLLECTION_NAME)

        limit = min(limit, 100)
        filters = self._build_filters(agent_context, user_scoped)

        result = await collection.query.fetch_objects(
            filters=filters,
            sort=Sort.by_property("created_at", ascending=False),
            limit=limit,
            offset=offset,
            return_properties=[
                "content", "org_id", "user_id", "dept_id", "source", "is_validated",
                "version", "is_active", "tags", "superseded_by_id",
                "created_at", "expires_at",
            ],
        )
        return [self._to_entry(obj) for obj in result.objects]

    async def get_entry(
        self,
        entry_id: str,
        agent_context: AgentContext,
    ) -> Optional[KnowledgeEntry]:
        """Fetch a single entry by UUID (enforces org-level tenant isolation)."""
        try:
            client = await get_weaviate_client()
            collection = client.collections.get(COLLECTION_NAME)

            obj = await collection.query.fetch_object_by_id(
                uuid=entry_id,
                return_properties=[
                    "content", "org_id", "user_id", "dept_id", "source", "is_validated",
                    "version", "is_active", "tags", "superseded_by_id",
                    "created_at", "expires_at",
                ],
            )
            if obj is None:
                return None

            if obj.properties.get("org_id") != str(agent_context.org_id):
                return None

            return self._to_entry(obj)

        except Exception as exc:
            logger.warning("get_entry failed for %s: %s", entry_id, exc)
            return None

    async def delete_entry(
        self,
        entry_id: str,
        agent_context: AgentContext,
    ) -> bool:
        """Soft-delete (set is_active=False). Returns True on success."""
        entry = await self.get_entry(entry_id, agent_context)
        if entry is None:
            return False

        client = await get_weaviate_client()
        collection = client.collections.get(COLLECTION_NAME)
        await collection.data.update(uuid=entry_id, properties={"is_active": False})
        return True

    # ── Internals ──────────────────────────────────────────────────────────

    @staticmethod
    def _build_filters(agent_context: AgentContext, user_scoped: bool):
        base = (
            Filter.by_property("org_id").equal(str(agent_context.org_id))
            & Filter.by_property("is_active").equal(True)
        )
        # Dept filter: return org-wide entries (dept_id="") OR dept-specific entries.
        if agent_context.dept_id:
            dept_filter = (
                Filter.by_property("dept_id").equal("")
                | Filter.by_property("dept_id").equal(str(agent_context.dept_id))
            )
            base = base & dept_filter
        if user_scoped:
            return base & (
                Filter.by_property("user_id").equal("")
                | Filter.by_property("user_id").equal(str(agent_context.user_id))
            )
        return base & Filter.by_property("user_id").equal("")

    async def _bm25_search(
        self,
        query: str,
        agent_context: AgentContext,
        user_scoped: bool,
    ) -> list[str]:
        """Fallback keyword search using Weaviate BM25."""
        try:
            client = await get_weaviate_client()
            collection = client.collections.get(COLLECTION_NAME)

            filters = self._build_filters(agent_context, user_scoped)
            result = await collection.query.bm25(
                query=query,
                filters=filters,
                limit=_TOP_K_RESULTS,
                return_properties=["content"],
            )
            return [str(obj.properties["content"]) for obj in result.objects]
        except Exception as exc:
            logger.warning("BM25 fallback also failed: %s", exc)
            return []

    async def _enforce_limits(
        self,
        agent_context: AgentContext,
        user_scoped: bool,
    ) -> None:
        """Raise ValueError if org or user storage limits are exceeded."""
        client = await get_weaviate_client()
        collection = client.collections.get(COLLECTION_NAME)

        org_filter = (
            Filter.by_property("org_id").equal(str(agent_context.org_id))
            & Filter.by_property("is_active").equal(True)
        )
        org_agg = await collection.aggregate.over_all(filters=org_filter, total_count=True)
        if (org_agg.total_count or 0) >= _MAX_ENTRIES_PER_ORG:
            logger.warning("KB org limit reached: org_id=%s", agent_context.org_id)
            await self._archive_oldest(agent_context, n=10)

        if user_scoped:
            user_filter = (
                Filter.by_property("org_id").equal(str(agent_context.org_id))
                & Filter.by_property("user_id").equal(str(agent_context.user_id))
                & Filter.by_property("is_active").equal(True)
            )
            user_agg = await collection.aggregate.over_all(
                filters=user_filter, total_count=True,
            )
            if (user_agg.total_count or 0) >= _MAX_ENTRIES_PER_USER:
                await self._archive_oldest_user(agent_context, n=5)

    async def _archive_oldest(self, agent_context: AgentContext, n: int) -> None:
        client = await get_weaviate_client()
        collection = client.collections.get(COLLECTION_NAME)

        org_filter = (
            Filter.by_property("org_id").equal(str(agent_context.org_id))
            & Filter.by_property("is_active").equal(True)
        )
        result = await collection.query.fetch_objects(
            filters=org_filter,
            sort=Sort.by_property("created_at", ascending=True),
            limit=n,
            return_properties=["is_active"],
        )
        for obj in result.objects:
            await collection.data.update(
                uuid=str(obj.uuid), properties={"is_active": False},
            )

    async def _archive_oldest_user(self, agent_context: AgentContext, n: int) -> None:
        client = await get_weaviate_client()
        collection = client.collections.get(COLLECTION_NAME)

        user_filter = (
            Filter.by_property("org_id").equal(str(agent_context.org_id))
            & Filter.by_property("user_id").equal(str(agent_context.user_id))
            & Filter.by_property("is_active").equal(True)
        )
        result = await collection.query.fetch_objects(
            filters=user_filter,
            sort=Sort.by_property("created_at", ascending=True),
            limit=n,
            return_properties=["is_active"],
        )
        for obj in result.objects:
            await collection.data.update(
                uuid=str(obj.uuid), properties={"is_active": False},
            )

    @staticmethod
    def _to_entry(obj) -> KnowledgeEntry:
        props = obj.properties
        user_id_raw = props.get("user_id", "")
        dept_id_raw = props.get("dept_id", "")
        return KnowledgeEntry(
            id=str(obj.uuid),
            content=props.get("content", ""),
            org_id=props.get("org_id", ""),
            user_id=user_id_raw if user_id_raw else None,
            source=props.get("source", ""),
            is_validated=bool(props.get("is_validated", False)),
            version=int(props.get("version", 1)),
            is_active=bool(props.get("is_active", True)),
            tags=list(props.get("tags", [])),
            superseded_by_id=props.get("superseded_by_id") or None,
            created_at=str(props["created_at"]) if props.get("created_at") else None,
            expires_at=str(props["expires_at"]) if props.get("expires_at") else None,
            dept_id=dept_id_raw if dept_id_raw else None,
        )
