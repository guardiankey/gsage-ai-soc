"""gSage AI — GLPI managerial dashboards.

Aggregates GLPI ticket data into managerial views: per group/status,
per technician workload, stalled tickets, SLA breaches, top requesters,
mean time-to-resolve and trend (created vs. solved).

Each view returns a ``truncated`` flag so the agent knows whether the
underlying search hit the per-view hard cap.

Required permission: ``glpi:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.ticket.glpi import _dashboard as views
from src.mcp_server.tools.soc.ticket.glpi._client import GLPIClient, GLPIError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_VIEWS = {
    "by_group_status",
    "by_technician",
    "stalled",
    "sla_breaches",
    "top_requesters",
    "mean_ttr",
    "trend",
}


class GlpiDashboardTool(BaseTool):
    """Compute managerial views over GLPI tickets.

    Pick a ``view``; supply optional ``groups`` (list of GLPI technician
    group IDs) to scope the aggregation. Each view has its own extra
    parameters:

    - ``by_group_status``: counts new/assigned/planned/waiting/solved_30d
      per technician group (or globally if ``groups`` is empty).
    - ``by_technician`` (``top_n``): active workload per technician.
    - ``stalled`` (``days_threshold``, ``top_n``): open tickets with
      ``date_mod`` older than N days.
    - ``sla_breaches`` (``window_days``, ``sla_field_id``): tickets past
      or near their SLA target date.
    - ``top_requesters`` (``days``, ``top_n``): biggest requester counts.
    - ``mean_ttr`` (``days``): mean and p95 time-to-resolve per group.
    - ``trend`` (``days``, ``granularity`` day|week): created vs. solved.

    Permission: ``glpi:read``.
    """

    name: ClassVar[str] = "glpi_dashboard"
    config_namespace: ClassVar[str] = "glpi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Managerial GLPI dashboards: per group/status, technician workload, "
        "stalled, SLA breaches, top requesters, mean TTR and trend"
    )
    category: ClassVar[str] = "itsm"
    permissions: ClassVar[list[str]] = ["glpi:read"]
    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 90
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
            "groups": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "maxItems": 20,
                "description": (
                    "Optional list of GLPI technician group IDs (field 8) "
                    "to scope the view. For most views only the first group "
                    "is honoured (GLPI flat criteria limitation); call the "
                    "tool once per group when a per-group breakdown is "
                    "needed. ``by_group_status`` iterates over every group "
                    "in the list."
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
            "sla_field_id": {
                "type": "integer",
                "minimum": 1,
                "maximum": 999,
                "description": (
                    "GLPI searchOption field id holding the SLA target "
                    "date (default 18 = time_to_resolve). Override if the "
                    "GLPI install uses a custom field id."
                ),
            },
            "granularity": {
                "type": "string",
                "enum": ["day", "week"],
                "description": "Bucket size for the 'trend' view.",
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "GLPI REST API base URL (overrides TOOL_GLPI__URL env var).",
            },
            "user_token": {
                "type": "string",
                "description": "GLPI user token (overrides TOOL_GLPI__USER_TOKEN env var).",
            },
            "app_token": {
                "type": "string",
                "description": "GLPI application token (overrides TOOL_GLPI__APP_TOKEN env var).",
            },
        },
        "additionalProperties": False,
    }
    config_defaults: ClassVar[dict] = {"url": "", "user_token": "", "app_token": ""}
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
                retryable=False,
            )

        groups: list[int] = list(params.get("groups") or [])

        try:
            async with GLPIClient(
                url=config.get("url") or None,
                user_token=config.get("user_token") or None,
                app_token=config.get("app_token") or None,
            ) as client:
                if view == "by_group_status":
                    data = await views.by_group_status(client, groups=groups or None)
                elif view == "by_technician":
                    data = await views.by_technician(
                        client,
                        groups=groups or None,
                        top_n=int(params.get("top_n") or 20),
                    )
                elif view == "stalled":
                    data = await views.stalled(
                        client,
                        groups=groups or None,
                        days_threshold=int(params.get("days_threshold") or 7),
                        top_n=int(params.get("top_n") or 50),
                    )
                elif view == "sla_breaches":
                    data = await views.sla_breaches(
                        client,
                        groups=groups or None,
                        window_days=int(params.get("window_days") or 7),
                        sla_field_id=int(params.get("sla_field_id") or 18),
                    )
                elif view == "top_requesters":
                    data = await views.top_requesters(
                        client,
                        groups=groups or None,
                        days=int(params.get("days") or 30),
                        top_n=int(params.get("top_n") or 20),
                    )
                elif view == "mean_ttr":
                    data = await views.mean_ttr(
                        client,
                        groups=groups or None,
                        days=int(params.get("days") or 30),
                    )
                elif view == "trend":
                    data = await views.trend(
                        client,
                        groups=groups or None,
                        days=int(params.get("days") or 14),
                        granularity=params.get("granularity") or "day",
                    )
                else:  # pragma: no cover — guarded by enum
                    return self._failure("INVALID_PARAMS", f"unknown view {view!r}")
        except GLPIError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.glpi_error or "GLPI_ERROR",
                str(exc),
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("glpi_dashboard: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={
                "view": view,
                "groups": groups,
                "params": {k: v for k, v in params.items() if k not in {"view"}},
                "data": data,
            },
            execution_time_ms=elapsed,
        )
