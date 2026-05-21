"""gSage AI — DRY runner for E-goi read tools.

All read tools share the same skeleton: open the SDK client, fetch rows
(possibly across multiple pages), summarise, build the agent payload
(with CSV overflow at 50 rows). This module factors that boilerplate
into a single coroutine so each tool stays declarative.
"""

from __future__ import annotations

import logging
import time
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Optional,
)

from src.mcp_server.tools.marketing.egoi import _query as Q
from src.mcp_server.tools.marketing.egoi._client import EgoiClient, EgoiError
from src.mcp_server.tools.result_export import (
    build_agent_payload,
    run_streaming_export,
    summarize,
)

if TYPE_CHECKING:
    from src.mcp_server.tools.base import BaseTool, ToolResult
    from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


# Fetcher: receives an open EgoiClient and returns a tuple of
# ``(rows, server_total_items)``. ``server_total_items`` is the *true*
# count reported by the upstream API (``total_items`` field on paginated
# responses) which may exceed ``len(rows)`` when ``max_rows`` capped the
# fetch. Return ``None`` when the endpoint does not report a total.
Fetcher = Callable[[EgoiClient], Awaitable[tuple[list[dict], Optional[int]]]]

# Streamer: like Fetcher but yields rows one-by-one to allow the runner
# to persist them incrementally (CSV-on-disk) instead of materialising
# the whole list. Each yield is ``(row_dict, server_total_or_None)``.
Streamer = Callable[
    [EgoiClient], AsyncIterator[tuple[dict, Optional[int]]]
]


def should_background_for_size(
    params: dict,
    *,
    rows_threshold: int,
    export_rows_threshold: Optional[int] = None,
) -> bool:
    """Decide whether a search invocation should be dispatched to Celery
    immediately, based on the requested ``max_rows`` and export flags.

    Heuristic (empirical for E-goi at ~300-500 rows/sec sustained):

    * ``max_rows >= rows_threshold`` → background (large enumeration).
    * Any export flag set AND ``max_rows >= export_rows_threshold`` →
      background (writing the artifact is part of the long path).

    Always returns False when ``max_rows`` is missing or non-positive
    (the schema default applies and is small).
    """
    try:
        max_rows = int(params.get("max_rows") or 0)
    except (TypeError, ValueError):
        max_rows = 0
    if max_rows <= 0:
        return False
    if max_rows >= rows_threshold:
        return True
    if export_rows_threshold is not None and max_rows >= export_rows_threshold:
        if bool(params.get("export_csv")):
            return True
    return False


async def run_search(
    tool: "BaseTool",
    *,
    agent_context: "AgentContext",
    config: dict,
    fetcher: Fetcher,
    filename_prefix: str,
    export_csv: bool = False,
    summary_group_by: Optional[list[str]] = None,
    summary_top_n: int = 10,
    extra_data: Optional[dict] = None,
    operation_label: str = "egoi search",
) -> "ToolResult":
    """Run an E-goi read tool's body with the canonical boilerplate.

    Parameters
    ----------
    tool :
        The :class:`BaseTool` instance (used for ``_success`` / ``_failure``
        helpers and as the file-store owner for artifacts).
    agent_context :
        Caller context (multi-tenant scoping, user, dept).
    config :
        Resolved tool configuration dict (E-goi credentials).
    fetcher :
        Async callable that takes an open :class:`EgoiClient` and returns
        the list of *already-normalised* dict rows. Pagination, filter
        building and normalisation are the fetcher's responsibility.
    filename_prefix :
        Prefix used for the CSV artifact persisted on overflow.
    export_csv :
        Forwarded to :func:`build_agent_payload`. CSV is forced anyway
        when the result set exceeds the inline preview cap.
    summary_group_by :
        Columns to aggregate in the analytical summary.
    summary_top_n :
        Top-N row count used by the summary aggregator.
    extra_data :
        Additional fields merged into the success payload (e.g. echoes
        of input filters, scope, total counts).
    operation_label :
        Short human-readable label used in error messages and logs.
    """
    t0 = time.monotonic()
    try:
        async with Q.build_client(config) as client:
            rows, server_total = await fetcher(client)
    except EgoiError as exc:
        elapsed = int((time.monotonic() - t0) * 1000)
        return tool._failure(  # type: ignore[attr-defined]
            exc.code,
            str(exc),
            retryable=Q.is_retryable_error(exc),
            execution_time_ms=elapsed,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("%s: unexpected error", operation_label)
        elapsed = int((time.monotonic() - t0) * 1000)
        return tool._failure(  # type: ignore[attr-defined]
            "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
        )

    smry = summarize(rows, group_by=summary_group_by, top_n=summary_top_n)
    agent_payload = await build_agent_payload(
        tool,
        rows=rows,
        export_csv=export_csv,
        export_json=False,
        filename_prefix=filename_prefix,
        agent_context=agent_context,
        preview_rows=Q.AGENT_PREVIEW_ROWS_EGOI,
    )

    # ``rows_total`` historically reported the number of *fetched* rows,
    # which confuses agents into thinking the dataset is small. Prefer
    # the upstream-reported total when available, and force overflow=true
    # whenever the server total exceeds what we returned.
    rows_fetched = int(agent_payload["rows_total"])
    overflow = bool(agent_payload["rows_overflow"])
    if isinstance(server_total, int) and server_total > rows_fetched:
        overflow = True

    elapsed = int((time.monotonic() - t0) * 1000)
    payload: dict[str, Any] = {
        "rows_total": (
            int(server_total) if isinstance(server_total, int) else rows_fetched
        ),
        "rows_fetched": rows_fetched,
        "server_total_items": (
            int(server_total) if isinstance(server_total, int) else None
        ),
        "rows_overflow": overflow,
        "rows": agent_payload["rows_preview"],
        "summary": smry,
        "artifacts": agent_payload["artifacts"],
        "agent_hint": agent_payload["agent_hint"],
    }
    if extra_data:
        payload.update(extra_data)
    return tool._success(payload, execution_time_ms=elapsed)  # type: ignore[attr-defined]


# ── Streaming variant (memory-bounded export for huge result sets) ─────────


async def run_search_streaming(
    tool: "BaseTool",
    *,
    agent_context: "AgentContext",
    config: dict,
    streamer: Streamer,
    filename_prefix: str,
    csv_columns: list[str],
    preview_rows: int = Q.AGENT_PREVIEW_ROWS_EGOI,
    summary_group_by: Optional[list[str]] = None,
    summary_top_n: int = 10,
    summary_sample_size: int = 20,
    extra_data: Optional[dict] = None,
    operation_label: str = "egoi search (stream)",
) -> "ToolResult":
    """E-goi adapter around :func:`result_export.run_streaming_export`.

    Opens the SDK client, instantiates the caller-supplied async streamer
    (which yields ``(row, server_total)`` pairs as it paginates the
    upstream API), and translates :class:`EgoiError` into the canonical
    retryable failure code. The actual CSV-on-disk machinery — tempfile
    directory, incremental summary, single-buffer upload — lives in the
    generic helper so other vendors (Trellix, OpenVAS, …) can reuse it.
    """
    t0 = time.monotonic()
    try:
        async with Q.build_client(config) as client:
            row_iter = streamer(client)
            return await run_streaming_export(
                tool,
                agent_context=agent_context,
                streamer=row_iter,
                filename_prefix=filename_prefix,
                csv_columns=csv_columns,
                preview_rows=preview_rows,
                summary_group_by=summary_group_by,
                summary_top_n=summary_top_n,
                summary_sample_size=summary_sample_size,
                extra_data=extra_data,
                operation_label=operation_label,
            )
    except EgoiError as exc:
        elapsed = int((time.monotonic() - t0) * 1000)
        return tool._failure(  # type: ignore[attr-defined]
            exc.code,
            str(exc),
            retryable=Q.is_retryable_error(exc),
            execution_time_ms=elapsed,
        )
