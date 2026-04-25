"""gSage AI — Weaviate async client singleton + schema initialization.

Single connection shared across all requests. Schema is created once on first connect.

Collection: ``KnowledgeBase``
  - Vectorized via text2vec-ollama (calls Ollama /api/embed internally).
  - Properties: content (vectorized), org_id, user_id, source, is_validated,
    version, is_active, tags, superseded_by_id, created_at, expires_at.
"""

from __future__ import annotations

import logging
from typing import Optional

import weaviate
from weaviate import WeaviateAsyncClient
from weaviate.classes.config import Configure, DataType, Property, Tokenization
from weaviate.classes.init import AdditionalConfig, Timeout

logger = logging.getLogger(__name__)

# Weaviate collection name for the knowledge base
COLLECTION_NAME = "KnowledgeBase"

# Bounded timeouts prevent queries from hanging forever on a half-closed /
# CLOSE-WAIT socket (which previously pinned the event loop at 100% CPU via
# anyio's ``_deliver_cancellation`` busy-loop when the hung task was
# cancelled). Values are intentionally conservative for a co-located DB.
# ``init`` is tunable via settings.weaviate_init_timeout to cope with slow
# gRPC startup under load.
def _build_additional_config() -> AdditionalConfig:
    from src.shared.config.settings import get_settings
    s = get_settings()
    return AdditionalConfig(
        timeout=Timeout(init=s.weaviate_init_timeout, query=30, insert=60),
    )

_client: Optional[WeaviateAsyncClient] = None
_schema_initialized: bool = False


async def get_weaviate_client() -> WeaviateAsyncClient:
    """Return the shared async Weaviate client.

    Creates connection on first call and initializes schema if needed.
    Reconnects automatically if the connection was lost.
    """
    global _client, _schema_initialized

    from src.shared.config.settings import get_settings
    settings = get_settings()

    if _client is None or not _client.is_connected():
        if _client is not None:
            try:
                await _client.close()
            except Exception:
                pass

        auth = (
            weaviate.auth.AuthApiKey(api_key=settings.weaviate_api_key)
            if settings.weaviate_api_key
            else None
        )
        # weaviate v4.7+ renamed connect_to_local_async → use_async_with_local
        # (factory only; must call await client.connect() separately)
        _client = weaviate.use_async_with_local(
            host=settings.weaviate_host,
            port=settings.weaviate_port,
            grpc_port=settings.weaviate_grpc_port,
            auth_credentials=auth,
            additional_config=_build_additional_config(),
            skip_init_checks=settings.weaviate_skip_init_checks,
        )
        await _client.connect()
        logger.info(
            "Weaviate client connected: %s:%d", settings.weaviate_host, settings.weaviate_port
        )
        _schema_initialized = False  # re-check schema after reconnect

    if not _schema_initialized and _client is not None:
        await _ensure_schema(_client)
        _schema_initialized = True

    assert _client is not None
    return _client


async def close_weaviate_client() -> None:
    """Close the shared async Weaviate client (call at application shutdown)."""
    global _client, _schema_initialized
    if _client is not None and _client.is_connected():
        await _client.close()
        logger.info("Weaviate client closed")
    _client = None
    _schema_initialized = False


async def _ensure_schema(client: WeaviateAsyncClient) -> None:
    """Create the KnowledgeBase collection if it does not already exist."""
    from src.shared.config.settings import get_settings
    settings = get_settings()

    if await client.collections.exists(COLLECTION_NAME):
        logger.debug("Weaviate collection '%s' exists — skipping schema init", COLLECTION_NAME)
        return

    logger.info("Creating Weaviate collection '%s'", COLLECTION_NAME)
    await client.collections.create(
        name=COLLECTION_NAME,
        # text2vec-ollama: Weaviate calls Ollama automatically on insert/nearText queries.
        # The gsage-ollama entrypoint creates nomic-embed-ctx8k from nomic-embed-text
        # with expanded num_ctx. See docker/ollama/entrypoint.sh.
        # vector_config replaces the deprecated vectorizer_config (weaviate-client >= 4.7)
        vector_config=Configure.Vectors.text2vec_ollama(
            name="default",
            api_endpoint=settings.ollama_base_url,
            model=settings.ollama_embedding_model,
            source_properties=["content"],
        ),
        properties=[
            # ── Vectorized property ───────────────────────────────────────
            Property(
                name="content",
                data_type=DataType.TEXT,
                tokenization=Tokenization.WORD,
                description="Knowledge content — this is the property that gets vectorized.",
            ),
            # ── Tenant isolation (not vectorized) ─────────────────────────
            Property(
                name="org_id",
                data_type=DataType.TEXT,
                skip_vectorization=True,
                tokenization=Tokenization.FIELD,
                description="Organization UUID (tenant isolation key).",
            ),
            Property(
                name="user_id",
                data_type=DataType.TEXT,
                skip_vectorization=True,
                tokenization=Tokenization.FIELD,
                description="User UUID. Empty string = org-level entry visible to all users.",
            ),
            Property(
                name="dept_id",
                data_type=DataType.TEXT,
                skip_vectorization=True,
                tokenization=Tokenization.FIELD,
                description="Department UUID. Empty string = org-wide entry visible to all depts.",
            ),
            # ── Origin metadata (not vectorized) ──────────────────────────
            Property(
                name="source",
                data_type=DataType.TEXT,
                skip_vectorization=True,
                description="Origin: user_request | agent_auto | admin.",
            ),
            Property(
                name="is_validated",
                data_type=DataType.BOOL,
                skip_vectorization=True,
                description="Reviewer-approved (prevents KB poisoning).",
            ),
            # ── Versioning (not vectorized) ────────────────────────────────
            Property(
                name="version",
                data_type=DataType.INT,
                skip_vectorization=True,
                description="Incremented on each update (append-only history).",
            ),
            Property(
                name="superseded_by_id",
                data_type=DataType.TEXT,
                skip_vectorization=True,
                tokenization=Tokenization.FIELD,
                description="Weaviate UUID of the newer version of this entry.",
            ),
            # ── Lifecycle (not vectorized) ─────────────────────────────────
            Property(
                name="is_active",
                data_type=DataType.BOOL,
                skip_vectorization=True,
                description="False = soft-deleted or superseded.",
            ),
            Property(
                name="tags",
                data_type=DataType.TEXT_ARRAY,
                skip_vectorization=True,
                description="Optional tags for categorization.",
            ),
            Property(
                name="created_at",
                data_type=DataType.DATE,
                skip_vectorization=True,
                description="Creation timestamp (RFC3339).",
            ),
            Property(
                name="expires_at",
                data_type=DataType.DATE,
                skip_vectorization=True,
                description="Optional TTL — entry deactivated by scheduled task after this date.",
            ),
        ],
    )
    logger.info("Weaviate collection '%s' created successfully", COLLECTION_NAME)
