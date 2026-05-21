"""gSage AI — E-goi campaign search tool.

Permission: ``egoi:read``
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.marketing.egoi import _query as Q
from src.mcp_server.tools.marketing.egoi import _run
from src.mcp_server.tools.marketing.egoi._client import EgoiClient
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


class EgoiCampaignSearchTool(BaseTool):
    """Search E-goi campaigns by channel, status, group, list and time-range.

    Returns the normalised campaign list. Use ``egoi_campaign_report``
    for per-campaign metrics.

    Permission: ``egoi:read``
    """

    name: ClassVar[str] = "egoi_campaign_search"
    config_namespace: ClassVar[Optional[str]] = "egoi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Search E-goi campaigns by channel, status, group, list and "
        "time range. Returns one row per campaign."
    )
    category: ClassVar[str] = "marketing"
    permissions: ClassVar[list[str]] = ["egoi:read"]

    rate_limit_per_minute: ClassVar[int] = 30
    # Auto-fallback to Celery if a sync execution exceeds ``timeout_seconds``.
    timeout_seconds: ClassVar[int] = 120
    background_threshold_seconds: ClassVar[Optional[int]] = 120
    background_timeout_seconds: ClassVar[Optional[int]] = 1800
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

    params_schema: ClassVar[Optional[dict]] = {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "enum": ["email", "sms", "voice", "push", "webpush"],
                "description": "Filter by delivery channel.",
            },
            "status": {
                "type": "string",
                "description": "Filter by campaign status (e.g. 'sent', 'draft', 'canceled').",
            },
            "list_id": {
                "type": "integer",
                "minimum": 1,
                "description": "Restrict to campaigns targeting this list.",
            },
            "group_id": {
                "type": "integer",
                "minimum": 1,
                "description": "Restrict to campaigns inside this campaign group.",
            },
            "internal_name": {
                "type": "string",
                "description": "Substring match on internal_name (server-side).",
            },
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": Q.HARD_MAX_ROWS,
                "default": Q.DEFAULT_MAX_ROWS,
            },
            "export_csv": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Persist all rows as a CSV file artifact. CSV is the "
                    "only supported export format for tabular results."
                ),
            },
        },
        "additionalProperties": False,
    }

    async def should_run_background(self, params: dict, config: dict) -> bool:
        if _run.should_background_for_size(
            params,
            rows_threshold=5000,
            export_rows_threshold=2000,
        ):
            return True
        return await super().should_run_background(params, config)

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        max_rows = Q.clamp_max_rows(params.get("max_rows"))
        group_id = params.get("group_id")
        filters: dict[str, str | int | None] = {
            "channel": (params.get("channel") or "").strip() or None,
            "status": (params.get("status") or "").strip() or None,
            "list_id": params.get("list_id"),
            "group_id": group_id if isinstance(group_id, int) and group_id > 0 else None,
            "internal_name": (params.get("internal_name") or "").strip() or None,
        }

        async def _fetch(client: EgoiClient) -> tuple[list[dict], Optional[int]]:
            async def page(offset: int, limit: int):
                kwargs: dict[str, Any] = {"offset": offset, "limit": limit}
                for k, v in filters.items():
                    if v is not None:
                        kwargs[k] = v
                return await client.get_all_campaigns(**kwargs)

            rows, server_total = await Q.iter_all_pages(
                page, max_rows=max_rows, normaliser=Q.normalize_campaign
            )
            return rows, server_total

        return await _run.run_search(
            self,
            agent_context=agent_context,
            config=config,
            fetcher=_fetch,
            filename_prefix="egoi_campaign_search",
            export_csv=bool(params.get("export_csv", False)),
            summary_group_by=["status", "type"],
            extra_data={"filters": {k: v for k, v in filters.items() if v is not None}},
            operation_label="egoi campaign_search",
        )
