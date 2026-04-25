"""gSage AI — Redis-backed buffer for Elasticsearch writes.

All Elasticsearch writes from any service are serialised as JSON and pushed
to a Redis list (``es:ingest:buffer``).  A dedicated Celery periodic task
(:mod:`src.backend_api.app.tasks.elasticsearch_ingest`) drains the list
every 60 seconds and bulk-inserts the documents.

This decouples ES latency/availability from the hot path of every service.

Usage::

    from src.shared.elasticsearch.redis_buffer import enqueue_for_es

    enqueue_for_es("tool-audit-log", {
        "@timestamp": "2026-04-18T12:00:00Z",
        "org_id": "...",
        ...
    })

``enqueue_for_es`` is **synchronous** and **fire-and-forget**: it works in
both sync and async contexts and never raises.  If Redis is unavailable the
call is silently dropped after logging a warning.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# Redis list key — all services push to this shared key.
ES_BUFFER_KEY = "es:ingest:buffer"


def _get_redis() -> Any:
    """Return a singleton sync Redis client (lazy init)."""
    global _redis_client  # noqa: PLW0603

    if _redis_client is not None:
        return _redis_client

    try:
        import redis as redis_lib  # noqa: PLC0415

        from src.shared.config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        _redis_client = redis_lib.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password or None,
            db=0,  # same DB as rate-limiting / general cache
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=False,  # raw bytes — faster for JSON blobs
        )
    except Exception as exc:
        log.warning("es_buffer: failed to create Redis client: %s", exc)

    return _redis_client


# Module-level singleton (None until first call).
_redis_client: Any = None


def enqueue_for_es(index_suffix: str, doc: dict[str, Any]) -> None:
    """Push *doc* onto the Elasticsearch ingest buffer in Redis.

    Parameters
    ----------
    index_suffix:
        Index family name without prefix or date, e.g. ``"agent-runs"``.
        The Celery consumer resolves the full index name as
        ``{prefix}{index_suffix}-{YYYY-MM-DD}`` using the document's own
        ``@timestamp`` field, so multi-day batches land in the correct index.
    doc:
        Document body.  ``@timestamp`` is added automatically (ISO-8601 UTC)
        if absent.
    """
    try:
        if "@timestamp" not in doc:
            doc["@timestamp"] = datetime.now(timezone.utc).isoformat()

        payload = json.dumps(
            {"idx": index_suffix, "doc": doc},
            ensure_ascii=False,
            default=str,
        ).encode()

        r = _get_redis()
        if r is None:
            return

        r.lpush(ES_BUFFER_KEY, payload)

    except Exception as exc:
        log.warning("es_buffer: enqueue failed (index=%s): %s", index_suffix, exc)
