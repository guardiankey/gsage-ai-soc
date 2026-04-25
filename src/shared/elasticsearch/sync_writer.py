"""gSage AI — Synchronous Elasticsearch writer for Celery workers.

Deprecated write path — now delegates to the Redis ingest buffer.

All calls to :func:`index_trace` enqueue the document in the Redis list
``es:ingest:buffer``.  The Celery task
:mod:`src.backend_api.app.tasks.elasticsearch_ingest` drains the buffer every
60 seconds and bulk-inserts into Elasticsearch.

The public function signature is unchanged for backwards compatibility.
"""

from __future__ import annotations

import logging
from typing import Any

from src.shared.elasticsearch.redis_buffer import enqueue_for_es

log = logging.getLogger(__name__)


def index_trace(index_suffix: str, doc: dict[str, Any]) -> None:
    """Enqueue *doc* for asynchronous insertion into ``{prefix}{index_suffix}-YYYY-MM-DD``.

    Best-effort: never raises.  Failures are logged as warnings.

    Parameters
    ----------
    index_suffix:
        The index family name without prefix or date, e.g. ``"agent-runs"``.
    doc:
        Document body.  If ``"@timestamp"`` is absent it is added automatically
        with the current UTC time.
    """
    enqueue_for_es(index_suffix, doc)
