"""gSage AI — MISP Dashboard tool.

Managerial aggregations over the MISP threat intelligence platform.
Provides multiple views: overview, timeline, top tags/galaxies/orgs,
threat level distribution, feed health, sightings trend, attribute type
distribution, warninglist hits, and ATT&CK matrix.

Required permission: ``misp:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.threat_intel.misp import _dashboard, _query as Q
from src.mcp_server.tools.soc.threat_intel.misp._client import MISPClient, MISPError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_DASHBOARD_VIEWS = sorted(_dashboard.DASHBOARD_VIEWS.keys())


class MispDashboardTool(BaseTool):
    """Managerial dashboard views over MISP data.

    Provides aggregated views: overview, events timeline, top tags,
    top galaxies, top organisations, threat level distribution,
    distribution map, feed health, sightings trend, attribute type
    distribution, warninglist hits and ATT&CK matrix.

    Permission: ``misp:read``
    """

    name: ClassVar[str] = "misp_dashboard"
    config_namespace: ClassVar[str] = "misp"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "MISP managerial dashboards: overview, timeline, top tags, "
        "top galaxies, top orgs, threat level, distribution, feed "
        "health, sightings, attribute types, warninglists, ATT&CK matrix"
    )
    category: ClassVar[str] = "threat_intel"
    permissions: ClassVar[list[str]] = ["misp:read"]

    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 120
    use_circuit_breaker: ClassVar[bool] = True

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    config_schema: ClassVar[Optional[dict]] = Q.MISP_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = Q.MISP_CONFIG_DEFAULTS

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
                "enum": _DASHBOARD_VIEWS,
                "description": "Dashboard view to render.",
            },
            "window_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 365,
                "default": 30,
                "description": "Time window for aggregations.",
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": _dashboard.DASHBOARD_TOP_N,
                "description": "Number of top entries to return.",
            },
            "org_id": {
                "type": "integer",
                "description": "Filter by specific organisation.",
            },
            "tag_filter": {
                "type": "string",
                "description": "Filter by tag (e.g. 'tlp:amber').",
            },
            "granularity": {
                "type": "string",
                "enum": ["day", "week", "month"],
                "default": "day",
                "description": "Time granularity for events_timeline.",
            },
            "mitre_domain": {
                "type": "string",
                "enum": ["enterprise", "mobile"],
                "default": "enterprise",
                "description": "MITRE domain for attack_matrix.",
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
        view = str(params.get("view", "")).strip()

        if not view or view not in _DASHBOARD_VIEWS:
            return self._failure("VALIDATION_ERROR", f"Unknown view: {view}", retryable=False)

        try:
            client = MISPClient(
                url=config["url"],
                api_key=config["api_key"],
                verify_cert=config.get("verify_cert", True),
            )

            window_days = int(params.get("window_days", 30))
            top_n = int(params.get("top_n", _dashboard.DASHBOARD_TOP_N))
            tag_filter = params.get("tag_filter")
            org_id = params.get("org_id")

            result_data: dict = {}
            if view == "overview":
                result_data = await _dashboard.view_overview(
                    client, window_days=window_days, tag_filter=tag_filter, org_id=org_id
                )
            elif view == "events_timeline":
                granularity = str(params.get("granularity", "day"))
                result_data = await _dashboard.view_events_timeline(
                    client, window_days=window_days, granularity=granularity,
                    tag_filter=tag_filter, org_id=org_id,
                )
            elif view == "top_tags":
                result_data = await _dashboard.view_top_tags(
                    client, top_n=top_n, window_days=window_days
                )
            elif view == "top_galaxies":
                result_data = await _dashboard.view_top_galaxies(
                    client, top_n=top_n, window_days=window_days
                )
            elif view == "top_organisations":
                result_data = await _dashboard.view_top_organisations(
                    client, top_n=top_n, window_days=window_days
                )
            elif view == "threat_level_distribution":
                result_data = await _dashboard.view_threat_level_distribution(
                    client, window_days=window_days
                )
            elif view == "distribution_map":
                result_data = await _dashboard.view_distribution_map(
                    client, window_days=window_days
                )
            elif view == "feed_health":
                result_data = await _dashboard.view_feed_health(client)
            elif view == "sightings_trend":
                result_data = await _dashboard.view_sightings_trend(
                    client, window_days=window_days, top_n=top_n
                )
            elif view == "attribute_type_distribution":
                result_data = await _dashboard.view_attribute_type_distribution(
                    client, window_days=window_days, top_n=top_n
                )
            elif view == "warninglist_hits":
                result_data = await _dashboard.view_warninglist_hits(client)
            elif view == "attack_matrix":
                mitre_domain = str(params.get("mitre_domain", "enterprise"))
                result_data = await _dashboard.view_attack_matrix(
                    client, window_days=window_days, mitre_domain=mitre_domain
                )

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return self._success(result_data, execution_time_ms=elapsed_ms)

        except MISPError as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code, exc.message,
                retryable=Q.is_retryable_error(exc),
                execution_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            log.exception("Unexpected error in misp_dashboard")
            return self._failure(
                "INTERNAL_ERROR", str(exc),
                retryable=False, execution_time_ms=elapsed_ms,
            )

    def _success(self, data: dict, execution_time_ms: int = 0) -> ToolResult:
        return ToolResult.success(
            data=data, tool_name=self.name, version=self.version,
            execution_time_ms=execution_time_ms,
        )

    def _failure(self, code: str, message: str, retryable: bool = False, execution_time_ms: int = 0) -> ToolResult:
        return ToolResult.failure(
            code=code, message=message, retryable=retryable,
            tool_name=self.name, version=self.version,
            execution_time_ms=execution_time_ms,
        )
