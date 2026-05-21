"""gSage AI — E-goi campaign-group search tool.

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


class EgoiCampaignGroupSearchTool(BaseTool):
    """List/search E-goi campaign groups (folders that group campaigns).

    Permission: ``egoi:read``
    """

    name: ClassVar[str] = "egoi_campaign_group_search"
    config_namespace: ClassVar[Optional[str]] = "egoi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "List E-goi campaign groups. Optional 'group_id' returns a "
        "single group; 'name' filters by substring (server-side)."
    )
    category: ClassVar[str] = "marketing"
    permissions: ClassVar[list[str]] = ["egoi:read"]

    rate_limit_per_minute: ClassVar[int] = 30
    # Auto-fallback to Celery if a sync execution exceeds ``timeout_seconds``.
    timeout_seconds: ClassVar[int] = 90
    background_threshold_seconds: ClassVar[Optional[int]] = 90
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
            "group_id": {
                "type": "integer",
                "minimum": 1,
                "description": "Return only the group with this id.",
            },
            "name": {
                "type": "string",
                "description": "Substring match on group name (server-side).",
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
                    "Persist all rows as a CSV file artifact. PREFER CSV "
                    "over JSON for tabular results."
                ),
            },
            "export_json": {
                "type": "boolean",
                "default": False,
                "description": "Persist all rows as JSON (only when explicitly asked).",
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
        max_rows = Q.clamp_max_rows(params.get("max_rows"))
        group_id = params.get("group_id")
        name = (params.get("name") or "").strip() or None

        async def _fetch(client: EgoiClient) -> tuple[list[dict], Optional[int]]:
            async def page(offset: int, limit: int):
                kwargs: dict[str, Any] = {"offset": offset, "limit": limit}
                if isinstance(group_id, int) and group_id > 0:
                    kwargs["group_id"] = group_id
                if name is not None:
                    kwargs["name"] = name
                return await client.get_all_campaign_groups(**kwargs)

            rows, server_total = await Q.iter_all_pages(
                page, max_rows=max_rows, normaliser=Q.normalize_campaign_group
            )
            return rows, server_total

        return await _run.run_search(
            self,
            agent_context=agent_context,
            config=config,
            fetcher=_fetch,
            filename_prefix="egoi_campaign_group_search",
            export_csv=bool(params.get("export_csv", False)),
            export_json=bool(params.get("export_json", False)),
            operation_label="egoi campaign_group_search",
        )
