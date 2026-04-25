"""gSage AI — per-tenant Knowledge builder.

Each organization gets its own Weaviate collection ``kb_{org_id}`` so there
is zero cross-tenant information leakage even when the vector index is shared
infrastructure.  The instances are cached in-process; each cache entry is
cheap (no live connection is held until a query is issued).

Usage::

    from src.backend_api.app.services.knowledge import build_knowledge

    # Inside a request handler:
    knowledge = build_knowledge(ctx.org_id)
    agent = Agent(..., knowledge=knowledge)
"""

from __future__ import annotations

import uuid
from typing import Optional

import weaviate
from agno.db.postgres.async_postgres import AsyncPostgresDb
from agno.knowledge.embedder.ollama import OllamaEmbedder
from agno.knowledge.knowledge import Knowledge
from agno.vectordb.weaviate.weaviate import Weaviate

from src.shared.config.settings import get_settings

import logging

log = logging.getLogger(__name__)


def _get_agno_db():
    """Lazy import to avoid circular import with agent_factory."""
    from src.backend_api.app.services.agent_factory import get_agno_db
    return get_agno_db()

# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

_KNOWLEDGE_LINKED_TO_PREFIX = "gsage_knowledge"


def knowledge_linked_to(org_id: uuid.UUID) -> str:
    """Stable ``linked_to`` discriminator stored in agno's knowledge DB.

    Must be used consistently everywhere: route handlers and Celery tasks.
    """
    return f"{_KNOWLEDGE_LINKED_TO_PREFIX}_{str(org_id).replace('-', '_')}"


# ---------------------------------------------------------------------------
# In-process cache keyed by org_id string
# ---------------------------------------------------------------------------

_knowledge_cache: dict[str, Knowledge] = {}


def _make_weaviate_client() -> weaviate.WeaviateClient:
    """Build a sync Weaviate client using the project's settings."""
    settings = get_settings()
    auth = (
        weaviate.auth.AuthApiKey(api_key=settings.weaviate_api_key)
        if settings.weaviate_api_key
        else None
    )
    return weaviate.connect_to_local(
        host=settings.weaviate_host,
        port=settings.weaviate_port,
        grpc_port=settings.weaviate_grpc_port,
        auth_credentials=auth,
        additional_config=weaviate.config.AdditionalConfig(
            timeout=weaviate.config.Timeout(
                init=settings.weaviate_init_timeout,
                query=60,
                insert=120,
            ),
        ),
        skip_init_checks=settings.weaviate_skip_init_checks,
    )


def _make_async_weaviate_client():
    """Build an async Weaviate client using the project's settings.

    agno's Weaviate.get_async_client() falls back to weaviate.use_async_with_local()
    with no arguments (hardcodes localhost:8080).  Pre-building the async client
    and assigning it to vector_db.async_client bypasses that default.
    """
    settings = get_settings()
    auth = (
        weaviate.auth.AuthApiKey(api_key=settings.weaviate_api_key)
        if settings.weaviate_api_key
        else None
    )
    return weaviate.use_async_with_local(
        host=settings.weaviate_host,
        port=settings.weaviate_port,
        grpc_port=settings.weaviate_grpc_port,
        auth_credentials=auth,
        additional_config=weaviate.config.AdditionalConfig(
            timeout=weaviate.config.Timeout(
                init=settings.weaviate_init_timeout,
                query=60,
                insert=120,
            ),
        ),
        skip_init_checks=settings.weaviate_skip_init_checks,
    )


def _patch_weaviate_update_metadata(vector_db: Weaviate) -> None:
    """Monkey-patch agno's Weaviate.update_metadata to use Weaviate v4 API.

    Fixes two agno 2.5.15 bugs:
    1. ``fetch_objects(where=...)`` is v3 syntax — v4 uses ``filters=``.
    2. ``meta_data`` is stored as ``DataType.TEXT`` (JSON string) in the
       collection schema, but the original code writes a plain dict, causing
       ``"not a string, but mapinterface {}"`` errors.
    """
    import json as _json
    from weaviate.collections.classes.filters import Filter as _Filter
    from types import MethodType

    def _fixed_update_metadata(self, content_id: str, metadata: dict) -> None:  # type: ignore[override]
        try:
            weaviate_client = self.get_client()
            collection = weaviate_client.collections.get(self.collection)

            query_result = collection.query.fetch_objects(
                filters=_Filter.by_property("content_id").equal(content_id),
                limit=1000,
            )

            if not query_result.objects:
                log.debug("update_metadata: no docs with content_id=%s", content_id)
                return

            updated_count = 0
            for obj in query_result.objects:
                current_props = obj.properties or {}
                updated_props = current_props.copy()

                # meta_data is stored as DataType.TEXT (JSON string) in the
                # Weaviate collection.  Deserialise, merge, then re-serialise.
                existing_md = updated_props.get("meta_data")
                if isinstance(existing_md, str):
                    try:
                        merged = _json.loads(existing_md)
                    except (ValueError, TypeError):
                        merged = {}
                elif isinstance(existing_md, dict):
                    merged = existing_md
                else:
                    merged = {}

                merged.update(metadata)
                updated_props["meta_data"] = _json.dumps(merged)

                collection.data.update(uuid=obj.uuid, properties=updated_props)
                updated_count += 1

            log.debug(
                "update_metadata: updated %d docs with content_id=%s",
                updated_count, content_id,
            )
        except Exception:
            log.exception("Error in update_metadata for content_id=%s", content_id)
            raise

    vector_db.update_metadata = MethodType(_fixed_update_metadata, vector_db)


def build_knowledge(
    org_id: uuid.UUID,
    contents_db: Optional[AsyncPostgresDb] = None,
) -> Knowledge:
    """Return a :class:`Knowledge` instance scoped to *org_id*.

    The Weaviate collection is named ``kb_<org_id_no_hyphens>`` to stay within
    Weaviate's class-name constraints (alphanumeric + underscore, starting with
    a capital letter is recommended but any letter works).

    The object is cached in-process so the same tenant always reuses the same
    instance within a worker process.
    """
    key = str(org_id)
    if key in _knowledge_cache:
        return _knowledge_cache[key]

    settings = get_settings()

    # Collection name: Weaviate requires names that start with a letter and
    # contain only alphanumerics + underscores.  UUID hyphens are replaced.
    collection = f"kb_{key.replace('-', '_')}"

    # Pass a pre-built sync client so agno doesn't fall back to localhost:8080
    # for synchronous operations.
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning, module="weaviate")
        vector_db = Weaviate(
            client=_make_weaviate_client(),
            collection=collection,
            embedder=OllamaEmbedder(
                id=settings.ollama_embedding_model,
                host=settings.ollama_base_url,
                dimensions=768,
                # num_ctx controls the context window Ollama uses for /api/embed.
                # Without this, Ollama falls back to its global default (2048),
                # truncating chunks and returning
                # "input length exceeds the context length" 400s for dense text.
                options={"num_ctx": settings.ollama_embed_num_ctx},
            ),
        )

    # agno's get_async_client() calls weaviate.use_async_with_local() with no
    # arguments, which hardcodes localhost:8080.  Pre-set async_client here so
    # that lazy initialisation uses the correct host/port from settings.
    vector_db.async_client = _make_async_weaviate_client()

    # --- agno 2.5.15 bug: update_metadata() calls fetch_objects(where=...) which
    # is Weaviate v3 syntax.  Weaviate v4 uses filters=.  Patch it in-place. ---
    _patch_weaviate_update_metadata(vector_db)

    knowledge = Knowledge(
        name=knowledge_linked_to(org_id),
        vector_db=vector_db,
        contents_db=contents_db if contents_db is not None else _get_agno_db(),
        isolate_vector_search=True,
    )

    _knowledge_cache[key] = knowledge
    log.debug("Built knowledge instance for org %s (collection=%s)", key, collection)
    return knowledge


def evict_knowledge_cache(org_id: Optional[uuid.UUID] = None) -> None:
    """Evict the in-process cache.

    If *org_id* is given only that tenant's entry is removed; otherwise the
    entire cache is cleared.  Useful in tests.
    """
    if org_id is not None:
        _knowledge_cache.pop(str(org_id), None)
    else:
        _knowledge_cache.clear()
