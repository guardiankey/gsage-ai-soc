"""gSage AI — Weaviate async client singleton + schema initialization.

Single connection shared across all requests. Schema is created once on first connect.

Collection: ``KnowledgeBase``
  - Vectorized via text2vec-ollama (calls Ollama /api/embed internally).
  - Properties: content (vectorized), org_id, user_id, source, is_validated,
    version, is_active, tags, superseded_by_id, created_at, expires_at.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import weaviate
from weaviate import WeaviateAsyncClient
from weaviate.classes.config import Configure, DataType, Property, Tokenization
from weaviate.classes.init import AdditionalConfig, Timeout
from weaviate.config import GrpcConfig

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
    # gRPC keepalive: actively probe the channel so half-closed sockets
    # (peer container restarted, NAT entry expired, etc.) are detected and
    # the channel is torn down quickly instead of becoming a black hole
    # that hangs queries until the kernel TCP timeout (minutes).
    grpc_options = [
        ("grpc.keepalive_time_ms", 30_000),       # ping every 30s when idle
        ("grpc.keepalive_timeout_ms", 10_000),    # wait 10s for ping ack
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.max_pings_without_data", 0),
        ("grpc.http2.min_time_between_pings_ms", 30_000),
        ("grpc.http2.min_ping_interval_without_data_ms", 30_000),
    ]
    return AdditionalConfig(
        timeout=Timeout(init=s.weaviate_init_timeout, query=30, insert=60),
        grpc_config=GrpcConfig(channel_options=grpc_options),
    )

# How often to actively probe the live endpoint (seconds).  The probe is
# rate-limited so we don't hit Weaviate on every single get_weaviate_client()
# call; it only runs if the last successful probe is older than this.
_LIVENESS_CHECK_INTERVAL = 30.0
# Bounded probe timeout — must be small so a stuck channel is detected
# quickly without itself becoming a source of cancel-loop traps.
_LIVENESS_PROBE_TIMEOUT = 5.0

_client: Optional[WeaviateAsyncClient] = None
_schema_initialized: bool = False
_last_liveness_ok_at: float = 0.0


async def _probe_liveness(client: WeaviateAsyncClient) -> bool:
    """Return True if the client answers ``is_live()`` within the probe budget.

    Any exception (gRPC error, timeout, connection error) is treated as a
    failed probe.  Callers are expected to drop the client and reconnect.
    """
    try:
        return bool(
            await asyncio.wait_for(client.is_live(), timeout=_LIVENESS_PROBE_TIMEOUT)
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # CancelledError can happen if a parent scope is being torn down;
        # propagate so we don't swallow legitimate cancellation, BUT only
        # if we are inside a cancelling scope. wait_for will raise
        # CancelledError if its own deadline was met after the inner task
        # was cancelled — treat that as a probe failure rather than a
        # propagation event.
        return False
    except Exception:
        logger.warning("Weaviate liveness probe failed", exc_info=True)
        return False


async def get_weaviate_client() -> WeaviateAsyncClient:
    """Return the shared async Weaviate client.

    Creates connection on first call and initializes schema if needed.
    Reconnects automatically if the connection was lost or fails a periodic
    liveness probe (gRPC channel half-closed, peer restarted, etc.).
    """
    global _client, _schema_initialized, _last_liveness_ok_at

    from src.shared.config.settings import get_settings
    settings = get_settings()

    # Periodic liveness probe — runs at most every _LIVENESS_CHECK_INTERVAL
    # seconds.  If the existing client is wedged we drop it before any
    # caller can attempt a query that would hang and trigger the anyio
    # cancel-loop trap on timeout.
    if _client is not None and _client.is_connected():
        now = time.monotonic()
        if (now - _last_liveness_ok_at) >= _LIVENESS_CHECK_INTERVAL:
            if await _probe_liveness(_client):
                _last_liveness_ok_at = now
            else:
                logger.warning(
                    "Weaviate liveness probe failed — forcing reconnect"
                )
                try:
                    await asyncio.wait_for(_client.close(), timeout=2)
                except Exception:
                    pass
                _client = None
                _schema_initialized = False

    if _client is None or not _client.is_connected():
        if _client is not None:
            try:
                await asyncio.wait_for(_client.close(), timeout=2)
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
        _last_liveness_ok_at = time.monotonic()

    if not _schema_initialized and _client is not None:
        await _ensure_schema(_client)
        _schema_initialized = True

    assert _client is not None
    return _client


async def reset_weaviate_client() -> None:
    """Force the next ``get_weaviate_client()`` call to reconnect.

    Call this from request handlers / background tasks that observe a gRPC
    error, ``asyncio.TimeoutError`` or a ``WeaviateQueryError`` against the
    current client.  It tears down the wedged channel so subsequent
    operations rebuild a fresh connection instead of hanging forever (which
    would otherwise trigger the anyio ``_deliver_cancellation`` busy-loop
    on the next ``wait_for``-based timeout).
    """
    global _client, _schema_initialized, _last_liveness_ok_at
    if _client is not None:
        try:
            await asyncio.wait_for(_client.close(), timeout=2)
        except Exception:
            logger.warning("Weaviate client close during reset failed", exc_info=True)
    _client = None
    _schema_initialized = False
    _last_liveness_ok_at = 0.0


async def close_weaviate_client() -> None:
    """Close the shared async Weaviate client (call at application shutdown)."""
    global _client, _schema_initialized, _last_liveness_ok_at
    if _client is not None and _client.is_connected():
        try:
            await asyncio.wait_for(_client.close(), timeout=5)
            logger.info("Weaviate client closed")
        except Exception:
            logger.warning("Weaviate client close at shutdown failed", exc_info=True)
    _client = None
    _schema_initialized = False
    _last_liveness_ok_at = 0.0


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
