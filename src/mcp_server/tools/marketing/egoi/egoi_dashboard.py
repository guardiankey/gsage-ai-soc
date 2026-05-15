"""gSage AI — Multi-view E-goi dashboard tool.

Permission: ``egoi:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.marketing.egoi import _dashboard, _query as Q
from src.mcp_server.tools.marketing.egoi._client import EgoiError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


class EgoiDashboardTool(BaseTool):
    """Multi-view aggregated dashboard for an E-goi account.

    Available views (see :data:`_dashboard.DASHBOARD_VIEWS`):

    - ``overview`` — total lists / contacts / campaigns.
    - ``top_lists`` — N largest lists by active contacts.
    - ``recent_campaigns`` — N most-recently updated campaigns.
    - ``delivery_funnel`` — sent / delivered / opens / clicks for one
      campaign (most recent if not specified).
    - ``engagement_trend`` — per-day opens/clicks for one campaign.

    Permission: ``egoi:read``
    """

    name: ClassVar[str] = "egoi_dashboard"
    config_namespace: ClassVar[str] = "egoi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Aggregated E-goi dashboard with multiple views (overview, "
        "top_lists, recent_campaigns, delivery_funnel, engagement_trend)."
    )
    category: ClassVar[str] = "marketing"
    permissions: ClassVar[list[str]] = ["egoi:read"]

    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 180
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    config_schema: ClassVar[Optional[dict]] = Q.EGOI_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = Q.EGOI_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {}
    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["view"],
        "properties": {
            "view": {
                "type": "string",
                "enum": _dashboard.DASHBOARD_VIEWS,
                "description": "Which dashboard view to compute.",
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": _dashboard.DASHBOARD_TOP_N,
                "description": "Cap for top_lists / recent_campaigns rows.",
            },
            "campaign_hash": {
                "type": "string",
                "description": (
                    "Target campaign for delivery_funnel / engagement_trend "
                    "(defaults to the most recent campaign)."
                ),
            },
        },
        "additionalProperties": False,
    }

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        view = str(params.get("view") or "").strip()
        if view not in _dashboard.VIEW_DISPATCH:
            return self._failure(
                "VALIDATION_ERROR",
                f"Unknown view '{view}'. Choose one of {_dashboard.DASHBOARD_VIEWS}.",
            )

        try:
            async with Q.build_client(config) as client:
                if view == "top_lists":
                    payload = await _dashboard.view_top_lists(
                        client, top_n=int(params.get("top_n") or _dashboard.DASHBOARD_TOP_N)
                    )
                elif view == "recent_campaigns":
                    payload = await _dashboard.view_recent_campaigns(
                        client, top_n=int(params.get("top_n") or _dashboard.DASHBOARD_TOP_N)
                    )
                elif view == "delivery_funnel":
                    payload = await _dashboard.view_delivery_funnel(
                        client,
                        campaign_hash=(params.get("campaign_hash") or "").strip() or None,
                    )
                elif view == "engagement_trend":
                    payload = await _dashboard.view_engagement_trend(
                        client,
                        campaign_hash=(params.get("campaign_hash") or "").strip() or None,
                    )
                else:
                    payload = await _dashboard.view_overview(client)
        except EgoiError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code,
                str(exc),
                retryable=Q.is_retryable_error(exc),
                execution_time_ms=elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("egoi_dashboard: unexpected error (view=%s)", view)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        result = {"view": view, **payload}
        return self._success(result, execution_time_ms=elapsed)
