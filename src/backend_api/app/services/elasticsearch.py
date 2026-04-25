"""gSage AI — Elasticsearch Tracer (Sprint 5.1).

Writes agent run traces to a daily-rotated Elasticsearch index with the
format:  ``{prefix}agno-traces-{YYYY-MM-DD}``

All writes are fire-and-forget (never block a run or raise).

Usage::

    from src.backend_api.app.services.elasticsearch import get_tracer

    tracer = get_tracer()
    await tracer.trace_run(
        org_id=str(ctx.org_id),
        user_id=str(ctx.user_id),
        session_id=session_id,
        agent_id=agent_id,
        run_id=agno_run_id,
        input_text=user_message,
        output_text=response_text,
        model=model_name,
        status="completed",
        duration_ms=elapsed_ms,
        token_count=total_tokens,
    )
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from src.shared.elasticsearch.redis_buffer import enqueue_for_es

log = logging.getLogger(__name__)


class ElasticsearchTracer:
    """Fire-and-forget tracer that writes Agno run events to Elasticsearch.

    One instance is created per process (singleton via :func:`get_tracer`).
    """

    def __init__(self, es_url: str, index_prefix: str) -> None:
        self._es_url = es_url.rstrip("/")
        self._index_prefix = index_prefix

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def trace_run(
        self,
        *,
        org_id: str,
        user_id: Optional[str],
        session_id: str,
        agent_id: str,
        run_id: Optional[str] = None,
        input_text: Optional[str] = None,
        output_text: Optional[str] = None,
        model: Optional[str] = None,
        status: str = "completed",
        duration_ms: Optional[int] = None,
        token_count: Optional[int] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        """Write a trace document to today's index.  Never raises."""
        doc: dict[str, Any] = {
            "@timestamp": datetime.now(timezone.utc).isoformat(),
            "organization_id": org_id,
            "user_id": user_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "run_id": run_id,
            "status": status,
            "model": model,
            "duration_ms": duration_ms,
            "token_count": token_count,
        }
        # Truncate large fields to avoid exceeding ES doc size limits
        if input_text:
            doc["input"] = input_text[:2000]
        if output_text:
            doc["output"] = output_text[:2000]
        if extra:
            doc["extra"] = extra

        # Remove None values to keep index clean
        doc = {k: v for k, v in doc.items() if v is not None}

        # Enqueue for async bulk insert via Celery flush task
        enqueue_for_es("agno-traces", doc)

    async def setup_index_template(self) -> None:
        """Idempotently create the Elasticsearch index template and ILM policy.

        Call once at application startup if ES is available.  Failures are
        logged and swallowed — the app starts regardless.

        Uses a short-lived synchronous client to avoid leaving an AsyncElasticsearch
        instance idle (which triggers a pycares CPU busy-loop).
        """
        try:
            from elasticsearch import Elasticsearch

            es = Elasticsearch(
                hosts=[self._es_url],
                request_timeout=10,
                retry_on_timeout=False,
                max_retries=0,
            )
        except ImportError:
            log.warning("elasticsearch package not installed — tracing disabled")
            return
        except Exception:
            log.warning("Failed to initialise ES client for template setup", exc_info=True)
            return

        try:
            policy_name = f"{self._index_prefix}traces-policy"
            template_name = f"{self._index_prefix}agno-traces"
            pattern = f"{self._index_prefix}agno-traces-*"

            # ILM Policy: hot → warm (7d) → delete (90d).
            # NOTE: No rollover action in hot phase — daily index rotation is handled by
            # the date-named index suffix.  Using ILM rollover would conflict because
            # rollover requires a write alias that these date-named indices do not have.
            ilm_policy = {
                "phases": {
                    "hot": {
                        "min_age": "0ms",
                        "actions": {
                            "set_priority": {"priority": 100},
                        },
                    },
                    "warm": {
                        "min_age": "7d",
                        "actions": {
                            "shrink": {"number_of_shards": 1},
                            "forcemerge": {"max_num_segments": 1},
                            "set_priority": {"priority": 50},
                        },
                    },
                    "delete": {
                        "min_age": "90d",
                        "actions": {"delete": {}},
                    },
                }
            }

            es.ilm.put_lifecycle(name=policy_name, policy=ilm_policy)

            # Index template with mappings
            index_template = {
                "index_patterns": [pattern],
                "template": {
                    "settings": {
                        "number_of_shards": 1,
                        "number_of_replicas": 0,
                        "index.lifecycle.name": policy_name,
                    },
                    "mappings": {
                        "properties": {
                            "@timestamp": {"type": "date"},
                            "organization_id": {"type": "keyword"},
                            "user_id": {"type": "keyword"},
                            "session_id": {"type": "keyword"},
                            "agent_id": {"type": "keyword"},
                            "run_id": {"type": "keyword"},
                            "input": {"type": "text"},
                            "output": {"type": "text"},
                            "duration_ms": {"type": "long"},
                            "token_count": {"type": "integer"},
                            "model": {"type": "keyword"},
                            "status": {"type": "keyword"},
                        }
                    },
                },
                "priority": 200,
            }

            es.indices.put_index_template(name=template_name, **index_template)
            log.info("ES trace template and ILM policy set up (prefix=%s)", self._index_prefix)

        except Exception:
            log.warning("ES trace setup failed (non-fatal)", exc_info=True)
        finally:
            es.close()

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_tracer: Optional[ElasticsearchTracer] = None


def get_tracer() -> ElasticsearchTracer:
    """Return the process-level :class:`ElasticsearchTracer` singleton."""
    global _tracer
    if _tracer is None:
        from src.shared.config.settings import get_settings
        log.info("Initializing ElasticsearchTracer singleton. ES URL: %s, index prefix: %s", get_settings().elasticsearch_url, get_settings().elasticsearch_trace_index_prefix)
        settings = get_settings()
        _tracer = ElasticsearchTracer(
            es_url=settings.elasticsearch_url,
            index_prefix=settings.elasticsearch_trace_index_prefix,
        )
    return _tracer
