"""gSage AI — Celery task: flush Redis ES ingest buffer.

This task is scheduled by Celery Beat every 60 seconds.  It atomically pops
all pending documents from the Redis list ``es:ingest:buffer``, groups them by
target index suffix, and bulk-inserts them into Elasticsearch.

Design decisions
----------------
- **Atomic drain**: uses a Redis pipeline to ``LRANGE`` + ``DEL`` so that
  a crash between the two operations can lose at most one batch (acceptable
  given the "perda mínima" tolerance agreed for this project).
- **Partial bulk failure**: failed docs are logged as warnings — they are NOT
  re-queued to avoid infinite retry loops.
- **Total ES failure** (unreachable): all docs are pushed back to the buffer
  via ``RPUSH`` so the next cycle can retry.
- **Daily index naming**: derived from each document's ``@timestamp`` field,
  so a batch spanning midnight lands in the correct day's index.
- **Max batch**: reads at most ``ES_MAX_BATCH`` docs per cycle (configurable).
  Remaining items stay in the buffer for the next run.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from celery import shared_task

from src.shared.elasticsearch.redis_buffer import ES_BUFFER_KEY

log = logging.getLogger(__name__)

# Maximum documents popped per cycle.  Prevents memory spikes on a large backlog.
ES_MAX_BATCH = 5_000


@shared_task(
    name="src.backend_api.app.tasks.elasticsearch_ingest.flush_es_buffer",
    bind=True,
    queue="elasticsearch",
    max_retries=0,  # Never auto-retry — the Beat schedule handles the next attempt
    ignore_result=True,
)
def flush_es_buffer(self: Any) -> None:  # noqa: ARG001
    """Drain Redis ES buffer and bulk-insert into Elasticsearch."""
    raw_items = _pop_from_buffer()
    if not raw_items:
        return

    # Deserialise JSON payloads
    entries: list[tuple[str, dict]] = []
    for raw in raw_items:
        try:
            payload = json.loads(raw)
            idx: str = payload["idx"]
            doc: dict = payload["doc"]
            entries.append((idx, doc))
        except Exception as exc:
            log.warning("flush_es_buffer: failed to parse buffer item: %s", exc)

    if not entries:
        return

    log.info("flush_es_buffer: flushing %d documents to Elasticsearch", len(entries))

    try:
        _bulk_insert(entries)
    except _ESUnavailableError:
        # ES completely unreachable — push items back for the next cycle
        _push_back_to_buffer(raw_items)
        log.warning(
            "flush_es_buffer: Elasticsearch unreachable — %d docs pushed back to buffer",
            len(raw_items),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pop_from_buffer() -> list[bytes]:
    """Atomically read up to ES_MAX_BATCH items and remove them from Redis.

    Uses a pipeline: LRANGE to read + LTRIM to remove the consumed slice.
    Items beyond ES_MAX_BATCH remain in the list for the next cycle.
    """
    try:
        import redis as redis_lib  # noqa: PLC0415

        from src.shared.config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        r = redis_lib.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password or None,
            db=0,
            socket_connect_timeout=3,
            socket_timeout=5,
            decode_responses=False,
        )
        with r.pipeline() as pipe:
            pipe.lrange(ES_BUFFER_KEY, 0, ES_MAX_BATCH - 1)
            # LTRIM keeps items from index ES_MAX_BATCH onward (i.e. removes
            # everything we just read).
            pipe.ltrim(ES_BUFFER_KEY, ES_MAX_BATCH, -1)
            results = pipe.execute()
        return results[0]  # list[bytes]
    except Exception as exc:
        log.error("flush_es_buffer: failed to read from Redis buffer: %s", exc)
        return []


def _push_back_to_buffer(raw_items: list[bytes]) -> None:
    """Re-push raw serialised items to the buffer tail (RPUSH) on ES failure."""
    try:
        import redis as redis_lib  # noqa: PLC0415

        from src.shared.config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        r = redis_lib.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password or None,
            db=0,
            socket_connect_timeout=3,
            socket_timeout=5,
            decode_responses=False,
        )
        r.rpush(ES_BUFFER_KEY, *raw_items)
    except Exception as exc:
        log.error("flush_es_buffer: failed to push items back to buffer: %s", exc)


class _ESUnavailableError(Exception):
    """Raised when the ES cluster cannot be reached at all."""


def _bulk_insert(entries: list[tuple[str, dict]]) -> None:
    """Group entries by target index and bulk-insert via the ES helpers API.

    Raises
    ------
    _ESUnavailableError
        When Elasticsearch is completely unreachable (connection error before
        any document is indexed).
    """
    try:
        from elasticsearch import Elasticsearch, ConnectionError as ESConnectionError  # noqa: PLC0415
        from elasticsearch.helpers import bulk as es_bulk  # noqa: PLC0415

        from src.shared.config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        es = Elasticsearch(
            hosts=[settings.elasticsearch_url],
            retry_on_timeout=True,
            max_retries=2,
            request_timeout=30,
        )
    except ImportError as exc:
        log.error("flush_es_buffer: elasticsearch package not installed: %s", exc)
        return

    # Group by index name (derived from index_suffix + @timestamp date)
    by_index: dict[str, list[dict]] = defaultdict(list)
    prefix = settings.elasticsearch_index_prefix

    for idx_suffix, doc in entries:
        try:
            ts = doc.get("@timestamp", "")
            # Parse date from ISO-8601 timestamp; fall back to today
            date_str = _date_from_timestamp(ts)
        except Exception:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        index_name = f"{prefix}{idx_suffix}-{date_str}"
        by_index[index_name].append(doc)

    # Perform bulk insert per index group
    total_failed = 0
    try:
        for index_name, docs in by_index.items():
            actions = [{"_index": index_name, "_source": doc} for doc in docs]
            try:
                _success, failed = es_bulk(
                    es,
                    actions,
                    raise_on_error=False,
                    raise_on_exception=False,
                )
                failed_list = failed if isinstance(failed, list) else []
                if failed_list:
                    total_failed += len(failed_list)
                    log.warning(
                        "flush_es_buffer: %d docs failed for index %s (partial bulk failure)",
                        len(failed_list),
                        index_name,
                    )
            except ESConnectionError as exc:
                raise _ESUnavailableError(str(exc)) from exc
            except Exception as exc:
                log.error(
                    "flush_es_buffer: bulk error for index %s: %s",
                    index_name,
                    exc,
                )
                total_failed += len(docs)
    finally:
        try:
            es.close()
        except Exception:
            pass

    if total_failed:
        log.warning(
            "flush_es_buffer: %d of %d documents failed (partial failure, not re-queued)",
            total_failed,
            len(entries),
        )
    else:
        log.info(
            "flush_es_buffer: successfully indexed %d documents",
            len(entries),
        )


def _date_from_timestamp(ts: str) -> str:
    """Extract ``YYYY-MM-DD`` from an ISO-8601 UTC string.  Falls back to today."""
    try:
        # Handle both "2026-04-18T12:00:00Z" and "2026-04-18T12:00:00+00:00"
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
