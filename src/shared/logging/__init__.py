"""Shared logging package for structured JSON logging and trace propagation.

Provides:
- ``JsonFormatter``            — emits log records as JSON to stdout
- ``TraceIdFilter``            — injects trace_id / org_id / user_id from contextvars
- ``ElasticsearchAppLogHandler`` — async-safe, batched ES ``app_logs`` writer
- ``configure_logging``        — one-shot setup for any service

Sensitive data rules (OWASP + PROMPT.md):
- Passwords, API keys, tokens are NEVER included in log output.
- ``_REDACT_KEYS`` lists patterns to redact from ``context`` dicts.

Usage::

    from src.shared.logging import configure_logging
    configure_logging(service_name="backend")
"""

from __future__ import annotations

import json
import logging
import os
import re
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

from src.shared.elasticsearch.redis_buffer import enqueue_for_es
from src.shared.logging.context import get_org_id, get_trace_id, get_user_id

__all__ = [
    "configure_logging",
    "JsonFormatter",
    "TraceIdFilter",
    "ElasticsearchAppLogHandler",
]

# ── Sensitive field redaction ──────────────────────────────────────────────
# Any key matching one of these patterns in a ``context`` dict will be
# replaced with "**REDACTED**" before the record is written.

_REDACT_PATTERNS: list[re.Pattern] = [
    re.compile(r, re.I)
    for r in [
        r"password",
        r"passwd",
        r"secret",
        r"api[_\-]?key",
        r"token",
        r"credential",
        r"auth",
        r"private[_\-]?key",
        r"encryption[_\-]?key",
        r"smtp[_\-]?pass",
        r"imap[_\-]?pass",
    ]
]


def _redact_dict(d: dict) -> dict:
    """Shallow-copy a dict, replacing sensitive values with REDACTED."""
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _redact_dict(v)
        elif any(p.search(str(k)) for p in _REDACT_PATTERNS):
            out[k] = "**REDACTED**"
        else:
            out[k] = v
    return out


# ── TraceIdFilter ──────────────────────────────────────────────────────────


class TraceIdFilter(logging.Filter):
    """Inject trace_id / org_id / user_id from contextvars into every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id()
        record.org_id   = get_org_id()
        record.user_id  = get_user_id()
        return True


# ── JsonFormatter ──────────────────────────────────────────────────────────


class JsonFormatter(logging.Formatter):
    """Format log records as newline-delimited JSON for Elasticsearch ingestion.

    Output schema matches ``app_logs`` index mapping (Phase 2):
        @timestamp, level, service, org_id, user_id, trace_id, message,
        context, error_stack
    """

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service = service_name

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        doc: dict[str, Any] = {
            "@timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":     record.levelname,
            "service":   self._service,
            "org_id":    getattr(record, "org_id",   "") or None,
            "user_id":   getattr(record, "user_id",  "") or None,
            "trace_id":  getattr(record, "trace_id", "unknown"),
            "message":   record.getMessage(),
        }

        # Extra context dict (caller can pass ``extra={"context": {...}}``)
        context: dict = {}
        raw_ctx = getattr(record, "context", None)
        if isinstance(raw_ctx, dict):
            context = _redact_dict(raw_ctx)

        # Merge standard extra fields (logger_name, module, lineno) into context
        context.setdefault("logger", record.name)
        context.setdefault("module", record.module)
        context.setdefault("line",   record.lineno)
        doc["context"] = context

        if record.exc_info:
            doc["error_stack"] = self.formatException(record.exc_info)

        return json.dumps(doc, ensure_ascii=False, default=str)


# ── ElasticsearchAppLogHandler ─────────────────────────────────────────────


class ElasticsearchAppLogHandler(logging.Handler):
    """Logging handler that enqueues ``app_logs`` documents into the Redis ES
    ingest buffer.

    The Celery task
    :mod:`src.backend_api.app.tasks.elasticsearch_ingest` drains the buffer
    every 60 seconds and bulk-inserts into Elasticsearch.

    Writes are non-blocking and fire-and-forget.  Failures are swallowed so
    log errors never disrupt the application.

    Args:
        es_url: Kept for API compatibility — no longer used for direct writes.
        index_prefix: Kept for API compatibility — the index prefix is read
            from settings by the Celery consumer.
        batch_size: Unused (batching handled by the Celery consumer).
        flush_interval: Unused (flush timing handled by Beat schedule).
        service_name: Value injected into the ``service`` field of each doc.
    """

    def __init__(
        self,
        es_url: str,
        index_prefix: str = "gsage-",
        batch_size: int = 50,
        flush_interval: float = 5.0,
        service_name: str = "",
    ) -> None:
        super().__init__()
        self._service = service_name

    # ── logging.Handler interface ──────────────────────────────────────────

    def emit(self, record: logging.LogRecord) -> None:
        """Build log document and push it to the Redis ES ingest buffer."""
        try:
            doc = self._record_to_doc(record)
            enqueue_for_es("app-logs", doc)
        except Exception:
            # Never let logging errors disrupt the application
            pass

    def close(self) -> None:
        super().close()

    # ── Internal helpers ───────────────────────────────────────────────────

    def _record_to_doc(self, record: logging.LogRecord) -> dict:
        context: dict = {}
        raw_ctx = getattr(record, "context", None)
        if isinstance(raw_ctx, dict):
            context = _redact_dict(raw_ctx)
        context.setdefault("logger", record.name)
        context.setdefault("module", record.module)
        context.setdefault("line",   record.lineno)

        doc: dict[str, Any] = {
            "@timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":     record.levelname,
            "service":   self._service or getattr(record, "service", "unknown"),
            "org_id":    getattr(record, "org_id",   "") or None,
            "user_id":   getattr(record, "user_id",  "") or None,
            "trace_id":  getattr(record, "trace_id", "unknown"),
            "message":   record.getMessage(),
            "context":   context,
        }
        if record.exc_info:
            doc["error_stack"] = "".join(traceback.format_exception(*record.exc_info))

        return doc


# ── configure_logging ──────────────────────────────────────────────────────


def configure_logging(
    service_name: str,
    level: str = "INFO",
    *,
    es_url: Optional[str] = None,
    es_index_prefix: str = "gsage-",
    enable_es_handler: bool = True,
) -> None:
    """Configure root logger with JSON formatting + optional ES handler.

    Call ONCE at service entry point (before any other logging).

    Args:
        service_name: One of: "backend", "mcp", "worker", "ui", "email_worker".
        level:        Minimum log level ("DEBUG", "INFO", "WARNING", "ERROR").
        es_url:       Elasticsearch URL. If None, reads from environment
                      ``ELASTICSEARCH_URL`` (default: ``http://elasticsearch:9200``).
        es_index_prefix: Index prefix for ``app_logs``.
        enable_es_handler: Set False to disable ES writing (useful in tests).
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # ── Trace filter + JSON formatter for stdout ───────────────────────────
    trace_filter = TraceIdFilter()
    json_formatter = JsonFormatter(service_name)

    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(json_formatter)
    stdout_handler.addFilter(trace_filter)
    stdout_handler.setLevel(numeric_level)

    handlers: list[logging.Handler] = [stdout_handler]

    # ── Optional Elasticsearch handler ────────────────────────────────────
    if enable_es_handler:
        resolved_es_url = es_url or os.environ.get(
            "ELASTICSEARCH_URL", "http://elasticsearch:9200"
        )
        es_handler = ElasticsearchAppLogHandler(
            es_url=resolved_es_url,
            index_prefix=es_index_prefix,
            service_name=service_name,
        )
        es_handler.addFilter(trace_filter)
        es_handler.setLevel(numeric_level)
        handlers.append(es_handler)

    # ── Root logger setup ─────────────────────────────────────────────────
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove any pre-existing handlers to avoid duplicate output
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    for h in handlers:
        root_logger.addHandler(h)

    # Silence noisy library loggers
    for noisy in (
        "elasticsearch",
        "elastic_transport",
        "httpx",
        "sqlalchemy.engine",
        "socketio",
        "engineio",
        "openai._base_client",
        "openai.http_client",
        "urllib3.connectionpool",
        "urllib3",
        "asyncio",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Propagate service name to context var so filters can read it
    from src.shared.logging.context import _service_var

    _service_var.set(service_name)
