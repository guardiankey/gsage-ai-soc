"""gSage AI — E-goi contact search tool (global + in-list scopes).

Permission: ``egoi:read``
"""

from __future__ import annotations

import logging
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.marketing.egoi import _query as Q
from src.mcp_server.tools.marketing.egoi import _run
from src.mcp_server.tools.marketing.egoi._client import EgoiClient
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


class EgoiContactSearchTool(BaseTool):
    """Search E-goi contacts globally or inside a specific mailing list.

    Two scopes:

    - ``global`` — uses ``GET /contacts-search`` to locate a contact by
      its email address across every list of the tenant.
    - ``list`` — uses ``GET /lists/{id}/contacts`` (optionally
      ``/segment/{seg_id}``) to enumerate contacts in a list/segment,
      with rich filtering.

    Permission: ``egoi:read``
    """

    name: ClassVar[str] = "egoi_contact_search"
    config_namespace: ClassVar[str] = "egoi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Search E-goi contacts. Use scope='global' to locate a contact "
        "by email across all lists, or scope='list' to enumerate "
        "contacts inside a specific list/segment."
    )
    category: ClassVar[str] = "marketing"
    permissions: ClassVar[list[str]] = ["egoi:read"]

    rate_limit_per_minute: ClassVar[int] = 30
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
        "required": ["scope"],
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["global", "list"],
                "description": (
                    "'global' searches by email across the whole tenant; "
                    "'list' enumerates contacts inside the given list."
                ),
            },
            "contact": {
                "type": "string",
                "description": (
                    "Email or phone (E.164) to locate. REQUIRED when "
                    "scope='global'."
                ),
            },
            "list_id": {
                "type": "integer",
                "minimum": 1,
                "description": "Target list. REQUIRED when scope='list'.",
            },
            "segment_id": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Optional segment inside the list (scope='list')."
                ),
            },
            "status": {
                "type": "string",
                "enum": ["active", "inactive", "removed", "unconfirmed"],
                "description": "Optional contact status filter (scope='list').",
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
        scope = str(params.get("scope") or "").strip()
        max_rows = Q.clamp_max_rows(params.get("max_rows"))

        if scope == "global":
            contact = (params.get("contact") or "").strip()
            if not contact:
                return self._failure(
                    "VALIDATION_ERROR",
                    "scope='global' requires 'contact' (email or phone)",
                )

            async def _fetch_global(client: EgoiClient) -> list[dict]:
                payload = await client.search_contacts(contact=contact)
                rows = [Q.normalize_contact(x) for x in Q.unwrap_items(payload)]
                if not rows and isinstance(payload, dict):
                    # search_contacts may return a single object
                    rows = [Q.normalize_contact(payload)] if payload.get(
                        "contact_id"
                    ) else []
                return rows[:max_rows]

            return await _run.run_search(
                self,
                agent_context=agent_context,
                config=config,
                fetcher=_fetch_global,
                filename_prefix="egoi_contact_search_global",
                export_csv=bool(params.get("export_csv", False)),
                export_json=bool(params.get("export_json", False)),
                summary_group_by=["status", "language"],
                extra_data={"scope": "global", "contact": contact},
                operation_label="egoi contact_search global",
            )

        if scope == "list":
            list_id = params.get("list_id")
            if not isinstance(list_id, int) or list_id <= 0:
                return self._failure(
                    "VALIDATION_ERROR",
                    "scope='list' requires a positive integer 'list_id'",
                )
            segment_id = params.get("segment_id")
            status = (params.get("status") or "").strip() or None

            async def _fetch_list(client: EgoiClient) -> list[dict]:
                if segment_id:
                    async def page(offset: int, limit: int):
                        return await client.get_all_contacts_by_segment(
                            list_id=int(list_id),
                            segment_id=int(segment_id),
                            offset=offset,
                            limit=limit,
                        )
                else:
                    async def page(offset: int, limit: int):
                        kwargs = {
                            "list_id": int(list_id),
                            "offset": offset,
                            "limit": limit,
                        }
                        if status:
                            kwargs["status"] = status
                        return await client.get_all_contacts(**kwargs)

                rows, _ = await Q.iter_all_pages(
                    page,
                    max_rows=max_rows,
                    normaliser=Q.normalize_contact,
                )
                # Tag the list_id on every row for downstream clarity.
                for r in rows:
                    r.setdefault("list_id", list_id)
                return rows

            return await _run.run_search(
                self,
                agent_context=agent_context,
                config=config,
                fetcher=_fetch_list,
                filename_prefix="egoi_contact_search_list",
                export_csv=bool(params.get("export_csv", False)),
                export_json=bool(params.get("export_json", False)),
                summary_group_by=["status", "language"],
                extra_data={
                    "scope": "list",
                    "list_id": list_id,
                    "segment_id": segment_id,
                    "status": status,
                },
                operation_label="egoi contact_search list",
            )

        return self._failure(
            "VALIDATION_ERROR",
            f"Unknown scope='{scope}'. Use 'global' or 'list'.",
        )
