"""Agent response filter pipeline.

Centralised post-processing layer applied to the LLM agent's outbound
response text. Filters declare a granularity (``fenced_block:<lang>`` or
``global``) so the pipeline can preserve streaming UX: text outside
relevant blocks flows through immediately, only fenced blocks are
buffered until they close.

Public entrypoints:

* :func:`apply_filters_to_text` — full-text mode (Teams, Telegram,
  email, sync REST).
* :func:`wrap_stream` — async-iterator wrapper for SSE streaming.
* :class:`FilterContext` — request-scoped context (org_id, interface,
  direction, optional db session).
"""
from __future__ import annotations

from src.shared.services.response_filter.base import (
    FilterContext,
    Granularity,
    ResponseFilter,
)
from src.shared.services.response_filter.pipeline import (
    StreamFilter,
    apply_filters_to_text,
    wrap_stream,
)

__all__ = [
    "FilterContext",
    "Granularity",
    "ResponseFilter",
    "StreamFilter",
    "apply_filters_to_text",
    "wrap_stream",
]
