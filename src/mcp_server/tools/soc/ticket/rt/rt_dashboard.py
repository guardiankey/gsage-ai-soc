"""gSage AI — RT managerial dashboards.

Aggregates RT ticket data into managerial views: per queue/status,
per owner workload, stalled tickets, SLA breaches, top requesters,
mean time-to-resolve and trend (created vs. resolved).

Each view returns a ``truncated`` flag so the agent knows whether the
underlying search hit the per-view hard cap.

Required permission: ``rt:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.ticket.rt import _dashboard as views
from src.mcp_server.tools.soc.ticket.rt._client import (
    RT_CONFIG_DEFAULTS,
    RT_CONFIG_SCHEMA,
    RTError,
    build_rt_client,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_VIEWS = {
    "by_queue_status",
    "by_owner",
    "stalled",
    "sla_breaches",
    "top_requesters",
    "mean_ttr",
    "trend",
}


class RTDashboardTool(BaseTool):
    """Compute managerial views over RT tickets.

    Pick a ``view``; supply optional ``queues`` (list of queue names) to
    scope the aggregation. Each view has its own extra parameters:

    - ``by_queue_status``: counts new/open/stalled/resolved_30d per queue.
    - ``by_owner`` (``top_n``): active workload per owner.
    - ``stalled`` (``days_threshold``, ``top_n``): tickets idle for at
      least N days.
    - ``sla_breaches`` (``window_days``): tickets past Due or due soon.
    - ``top_requesters`` (``days``, ``top_n``): biggest requester counts.
    - ``mean_ttr`` (``days``): mean and p95 time-to-resolve per queue.
    - ``trend`` (``days``, ``granularity`` day|week): created vs. resolved.

    Permission: ``rt:read``.
    """

    name: ClassVar[str] = "rt_dashboard"
    config_namespace: ClassVar[str] = "rt"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Managerial RT dashboards: per queue/status, owner workload, stalled, "
        "SLA breaches, top requesters, mean TTR and trend"
    )
    category: ClassVar[str] = "itsm"
    permissions: ClassVar[list[str]] = ["rt:read"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    audit_field_mapping: ClassVar[dict] = {}

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["view"],
        "properties": {
            "view": {
                "type": "string",
                "enum": sorted(_VIEWS),
                "description": "Which managerial aggregation to compute.",
            },
            "queues": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
                "description": (
                    "Optional list of queue names to scope the view. "
                    "Required by 'by_queue_status' / 'by_owner' / "
                    "'mean_ttr' / 'trend' / 'top_requesters' to keep the "
                    "result set bounded."
                ),
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Row cap for views that produce a leaderboard.",
            },
            "days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 365,
                "description": (
                    "Look-back window in days. Used by top_requesters, "
                    "mean_ttr, trend."
                ),
            },
            "days_threshold": {
                "type": "integer",
                "minimum": 1,
                "maximum": 365,
                "description": "Idle days threshold for the 'stalled' view.",
            },
            "window_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 90,
                "description": "Forward window for 'sla_breaches'.",
            },
            "granularity": {
                "type": "string",
                "enum": ["day", "week"],
                "description": "Bucket size for the 'trend' view.",
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = RT_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = RT_CONFIG_DEFAULTS
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        view = params.get("view")
        if view not in _VIEWS:
            return self._failure(
                "INVALID_PARAMS",
                f"view must be one of {sorted(_VIEWS)}; got {view!r}.",
            )

        queues = params.get("queues") or []
        if not queues and view in {
            "by_queue_status",
            "by_owner",
            "top_requesters",
            "mean_ttr",
            "trend",
            "sla_breaches",
        }:
            return self._failure(
                "INVALID_PARAMS",
                f"view={view!r} requires a non-empty 'queues' list to bound "
                "the result set (RT scoping).",
            )

        try:
            async with build_rt_client(config) as client:
                if view == "by_queue_status":
                    data = await views.by_queue_status(client, queues=queues)
                elif view == "by_owner":
                    data = await views.by_owner(
                        client, queues=queues, top_n=int(params.get("top_n") or 20)
                    )
                elif view == "stalled":
                    data = await views.stalled(
                        client,
                        queues=queues or None,
                        days_threshold=int(params.get("days_threshold") or 7),
                        top_n=int(params.get("top_n") or 50),
                    )
                elif view == "sla_breaches":
                    data = await views.sla_breaches(
                        client,
                        queues=queues,
                        window_days=int(params.get("window_days") or 7),
                    )
                elif view == "top_requesters":
                    data = await views.top_requesters(
                        client,
                        queues=queues,
                        days=int(params.get("days") or 30),
                        top_n=int(params.get("top_n") or 20),
                    )
                elif view == "mean_ttr":
                    data = await views.mean_ttr(
                        client, queues=queues, days=int(params.get("days") or 30)
                    )
                elif view == "trend":
                    data = await views.trend(
                        client,
                        queues=queues,
                        days=int(params.get("days") or 14),
                        granularity=params.get("granularity") or "day",
                    )
                else:  # pragma: no cover — guarded by enum
                    return self._failure("INVALID_PARAMS", f"unknown view {view!r}")
        except RTError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(exc.code, str(exc), execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("rt_dashboard: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={
                "view": view,
                "queues": queues,
                "params": {k: v for k, v in params.items() if k not in {"view"}},
                "data": data,
            },
            execution_time_ms=elapsed,
        )
