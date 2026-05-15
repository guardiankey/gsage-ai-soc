"""gSage AI — E-goi email-campaign report tool.

Permission: ``egoi:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.marketing.egoi import _query as Q
from src.mcp_server.tools.marketing.egoi import _charts
from src.mcp_server.tools.marketing.egoi._client import EgoiError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


_BREAKDOWN_KEYS = ("date", "weekday", "hour", "location", "domain", "url", "reader")
_CHART_KINDS = ("none", "bar_daily", "bar_by_metric", "stacked_daily")


class EgoiCampaignReportTool(BaseTool):
    """Fetch the email-campaign report for a single ``campaign_hash``.

    Returns the totals/overall metrics and any requested breakdowns
    (per-day, per-domain, per-url, etc.). Optionally generates a
    Mermaid ``xychart-beta`` visualisation.

    Permission: ``egoi:read``
    """

    name: ClassVar[str] = "egoi_campaign_report"
    config_namespace: ClassVar[str] = "egoi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Fetch the E-goi email-campaign report (overall totals + "
        "selectable breakdowns) with an optional Mermaid chart."
    )
    category: ClassVar[str] = "marketing"
    permissions: ClassVar[list[str]] = ["egoi:read"]

    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 120
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
        "required": ["campaign_hash"],
        "properties": {
            "campaign_hash": {
                "type": "string",
                "description": "Hash of the email campaign to inspect.",
            },
            "breakdowns": {
                "type": "array",
                "items": {"type": "string", "enum": list(_BREAKDOWN_KEYS)},
                "default": [],
                "description": (
                    "Optional list of breakdown sections to request. "
                    "Empty array means 'all sections the campaign reports "
                    "natively'."
                ),
            },
            "chart": {
                "type": "string",
                "enum": list(_CHART_KINDS),
                "default": "none",
                "description": "Optional Mermaid chart kind to render.",
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
        campaign_hash = str(params.get("campaign_hash") or "").strip()
        if not campaign_hash:
            return self._failure("VALIDATION_ERROR", "campaign_hash is required")
        breakdowns: list[str] = list(params.get("breakdowns") or [])
        chart_kind = str(params.get("chart") or "none")

        # If specific breakdowns were requested, pass them as True; otherwise leave None.
        kwargs: dict = {}
        if breakdowns:
            for k in _BREAKDOWN_KEYS:
                if k in breakdowns:
                    kwargs[k] = True

        try:
            async with Q.build_client(config) as client:
                report = await client.get_email_report(
                    campaign_hash=campaign_hash, **kwargs
                )
        except EgoiError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code,
                str(exc),
                retryable=Q.is_retryable_error(exc),
                execution_time_ms=elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("egoi_campaign_report: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        report = report if isinstance(report, dict) else {}
        overall = report.get("totals") or report.get("overall") or {}
        # Collect breakdown rows by key (best-effort — keys depend on the
        # E-goi schema version).
        breakdown_payload: dict[str, list[dict]] = {}
        for key in _BREAKDOWN_KEYS:
            api_key = f"by_{key}" if key != "date" else "by_date"
            rows = list(Q.iter_email_breakdown(report, api_key))
            if rows:
                breakdown_payload[key] = rows

        mermaid_chart: Optional[str] = None
        if chart_kind == "bar_by_metric":
            mermaid_chart = _charts.build_bar_by_metric(
                overall, title=f"Campaign {campaign_hash[:12]} totals"
            )
        elif chart_kind == "bar_daily":
            mermaid_chart = _charts.build_bar_daily(
                breakdown_payload.get("date", []),
                title=f"Campaign {campaign_hash[:12]} daily",
            )
        elif chart_kind == "stacked_daily":
            mermaid_chart = _charts.build_stacked_daily(
                breakdown_payload.get("date", []),
                title=f"Campaign {campaign_hash[:12]} daily (cumulative)",
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            {
                "campaign_hash": campaign_hash,
                "overall": overall,
                "breakdowns": breakdown_payload,
                "mermaid_chart": mermaid_chart,
                "raw_report_keys": sorted(report.keys()),
            },
            execution_time_ms=elapsed,
        )
