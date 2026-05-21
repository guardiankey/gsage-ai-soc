"""Hardcoded, always-active list of outbound response filters.

To add a new filter: implement the :class:`ResponseFilter` protocol and
append an instance to :data:`OUTBOUND_FILTERS`. Filters are applied in
list order; for streaming mode all filters of granularity
``fenced_block:<lang>`` for the same ``<lang>`` are chained on each
closed block.
"""
from __future__ import annotations

from src.shared.services.response_filter.base import ResponseFilter
from src.shared.services.response_filter.filters.mermaid_timeline import (
    MermaidTimelineTimeFilter,
)

OUTBOUND_FILTERS: tuple[ResponseFilter, ...] = (
    MermaidTimelineTimeFilter(),
)
