"""gSage AI — DRY runner for E-goi read tools.

All read tools share the same skeleton: open the SDK client, fetch rows
(possibly across multiple pages), summarise, build the agent payload
(with CSV overflow at 50 rows). This module factors that boilerplate
into a single coroutine so each tool stays declarative.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from src.mcp_server.tools.marketing.egoi import _query as Q
from src.mcp_server.tools.marketing.egoi._client import EgoiClient, EgoiError
from src.mcp_server.tools.result_export import build_agent_payload, summarize

if TYPE_CHECKING:
    from src.mcp_server.tools.base import BaseTool, ToolResult
    from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


# Fetcher: receives an open EgoiClient, returns a list of already-normalised rows.
Fetcher = Callable[[EgoiClient], Awaitable[list[dict]]]


async def run_search(
    tool: "BaseTool",
    *,
    agent_context: "AgentContext",
    config: dict,
    fetcher: Fetcher,
    filename_prefix: str,
    export_csv: bool = False,
    export_json: bool = False,
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
        Prefix used for the CSV/JSON artifacts persisted on overflow.
    export_csv, export_json :
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
            rows = await fetcher(client)
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
        export_json=export_json,
        filename_prefix=filename_prefix,
        agent_context=agent_context,
        preview_rows=Q.AGENT_PREVIEW_ROWS_EGOI,
    )

    elapsed = int((time.monotonic() - t0) * 1000)
    payload: dict[str, Any] = {
        "rows_total": agent_payload["rows_total"],
        "rows_overflow": agent_payload["rows_overflow"],
        "rows": agent_payload["rows_preview"],
        "summary": smry,
        "artifacts": agent_payload["artifacts"],
        "agent_hint": agent_payload["agent_hint"],
    }
    if extra_data:
        payload.update(extra_data)
    return tool._success(payload, execution_time_ms=elapsed)  # type: ignore[attr-defined]
