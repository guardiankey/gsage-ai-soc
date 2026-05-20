"""gSage AI — E-goi list (mailing-list) search tool.

Permission: ``egoi:read``
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.marketing.egoi import _query as Q
from src.mcp_server.tools.marketing.egoi import _run
from src.mcp_server.tools.marketing.egoi._client import EgoiClient
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


class EgoiListSearchTool(BaseTool):
    """Search mailing lists on the configured E-goi account.

    Returns lists with their public/internal name, language and contact
    stats (active / inactive / unconfirmed / removed). Supports
    free-text filtering on ``internal_name`` and ``public_name``.

    Permission: ``egoi:read``
    """

    name: ClassVar[str] = "egoi_list_search"
    config_namespace: ClassVar[str] = "egoi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Search E-goi mailing lists. Returns one row per list with "
        "contact stats. Use this before any contact-level operation to "
        "discover the target ``list_id``."
    )
    category: ClassVar[str] = "marketing"
    permissions: ClassVar[list[str]] = ["egoi:read"]

    rate_limit_per_minute: ClassVar[int] = 30
    # Auto-fallback to Celery if a sync execution exceeds ``timeout_seconds``.
    # ``include_stats=True`` triggers one extra API call per list which
    # can dominate runtime on accounts with many lists.
    timeout_seconds: ClassVar[int] = 120
    background_threshold_seconds: ClassVar[Optional[int]] = 120
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
        "properties": {
            "internal_name": {
                "type": "string",
                "description": "Substring match on the list's internal name.",
            },
            "public_name": {
                "type": "string",
                "description": "Substring match on the list's public-facing name.",
            },
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": Q.HARD_MAX_ROWS,
                "default": Q.DEFAULT_MAX_ROWS,
            },
            "include_stats": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Fetch contact_stats (active/inactive/unconfirmed/"
                    "removed) per list. Requires one extra API call per "
                    "list returned, so leave off for quick discovery."
                ),
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

    async def should_run_background(self, params: dict, config: dict) -> bool:
        # ``include_stats`` adds one API call per list; if the caller
        # asks for stats AND a sizeable batch, run in background to
        # avoid blocking the chat layer. Otherwise apply the standard
        # row-based heuristic.
        include_stats = bool(params.get("include_stats"))
        if include_stats and _run.should_background_for_size(
            params,
            rows_threshold=100,
            export_rows_threshold=50,
        ):
            return True
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
        internal_filter = (params.get("internal_name") or "").strip().lower()
        public_filter = (params.get("public_name") or "").strip().lower()
        max_rows = Q.clamp_max_rows(params.get("max_rows"))
        include_stats = bool(params.get("include_stats", False))

        def _match(row: dict) -> bool:
            if internal_filter and internal_filter not in (
                str(row.get("internal_name") or "").lower()
            ):
                return False
            if public_filter and public_filter not in (
                str(row.get("public_name") or "").lower()
            ):
                return False
            return True

        async def _fetch(client: EgoiClient) -> tuple[list[dict], Optional[int]]:
            async def page(offset: int, limit: int):
                return await client.get_all_lists(offset=offset, limit=limit)

            rows, server_total = await Q.iter_all_pages(
                page, max_rows=max_rows, normaliser=Q.normalize_list
            )
            rows = [r for r in rows if _match(r)]

            if include_stats and rows:
                # The /lists endpoint omits contact_stats; fan out one
                # /lists/{id} call per row to populate the counters.
                async def _fetch_detail(list_id: Any) -> dict:
                    try:
                        detail = await client.get_list(int(list_id))
                    except Exception:  # noqa: BLE001 — keep partial data
                        return {}
                    return Q.normalize_list(detail) if isinstance(detail, dict) else {}

                details = await asyncio.gather(
                    *(_fetch_detail(r.get("list_id")) for r in rows),
                    return_exceptions=False,
                )
                for row, detail in zip(rows, details):
                    for key in (
                        "contacts_active",
                        "contacts_inactive",
                        "contacts_unconfirmed",
                        "contacts_removed",
                    ):
                        if detail.get(key) is not None:
                            row[key] = detail[key]
            return rows, server_total

        return await _run.run_search(
            self,
            agent_context=agent_context,
            config=config,
            fetcher=_fetch,
            filename_prefix="egoi_list_search",
            export_csv=bool(params.get("export_csv", False)),
            export_json=bool(params.get("export_json", False)),
            summary_group_by=["language"],
            operation_label="egoi list_search",
        )
