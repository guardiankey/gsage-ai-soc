"""gSage AI — E-goi contact search tool (global + in-list scopes).

Permission: ``egoi:read``
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.marketing.egoi import _query as Q
from src.mcp_server.tools.marketing.egoi import _run
from src.mcp_server.tools.marketing.egoi import _tags
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
    config_namespace: ClassVar[Optional[str]] = "egoi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Search E-goi contacts. Use scope='global' to locate a contact "
        "by email across all lists, or scope='list' to enumerate "
        "contacts inside a specific list/segment."
    )
    category: ClassVar[str] = "marketing"
    permissions: ClassVar[list[str]] = ["egoi:read"]

    rate_limit_per_minute: ClassVar[int] = 30
    # Sync timeout doubles as the auto-fallback trigger when
    # ``background_threshold_seconds`` is set. We keep it under the chat
    # layer's tool timeout so the agent gets a 'background' status
    # instead of a timeout error.
    timeout_seconds: ClassVar[int] = 120
    background_threshold_seconds: ClassVar[Optional[int]] = 120
    # Large lists (50k+ contacts) need plenty of room when paging at 200/page
    # with occasional RemoteDisconnected retries.  30 min keeps us safely below
    # Celery's hard task limit while covering realistic tenant volumes.
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
                    "Persist all rows as a CSV file artifact. CSV is the "
                    "only supported export format for tabular results."
                ),
            },
            "resolve_tags": {
                "type": "boolean",
                "default": True,
                "description": (
                    "When true, each row's 'tags' field is enriched into "
                    "[{tag_id, name}, ...] by resolving tag ids against "
                    "GET /tags. Adds one cached lookup per execution. "
                    "Set false for very large enumerations where the "
                    "raw id list is acceptable."
                ),
            },
        },
        "additionalProperties": False,
    }

    async def should_run_background(self, params: dict, config: dict) -> bool:
        # E-goi contact pages are ~200 rows and the API sustains roughly
        # 300-500 rows/sec. Dispatch immediately when the requested
        # batch is large or a sizeable export was requested.
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
        scope = str(params.get("scope") or "").strip()
        max_rows = Q.clamp_max_rows(params.get("max_rows"))
        resolve_tags_flag = bool(params.get("resolve_tags", True))

        if scope == "global":
            contact = (params.get("contact") or "").strip()
            if not contact:
                return self._failure(
                    "VALIDATION_ERROR",
                    "scope='global' requires 'contact' (email or phone)",
                )

            async def _fetch_global(client: EgoiClient) -> tuple[list[dict], Optional[int]]:
                payload = await client.search_contacts(contact=contact)
                tag_index = (
                    await _tags.get_tag_index(client) if resolve_tags_flag else None
                )
                rows = [
                    Q.normalize_contact(x, tag_index=tag_index)
                    for x in Q.unwrap_items(payload)
                ]
                if not rows and isinstance(payload, dict):
                    # search_contacts may return a single object
                    rows = (
                        [Q.normalize_contact(payload, tag_index=tag_index)]
                        if payload.get("contact_id")
                        else []
                    )
                return rows[:max_rows], Q.total_items(payload)

            return await _run.run_search(
                self,
                agent_context=agent_context,
                config=config,
                fetcher=_fetch_global,
                filename_prefix="egoi_contact_search_global",
                export_csv=bool(params.get("export_csv", False)),
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
            segment_id_raw = params.get("segment_id")
            segment_id = (
                segment_id_raw
                if isinstance(segment_id_raw, int) and segment_id_raw > 0
                else None
            )
            status = (params.get("status") or "").strip() or None

            async def _fetch_list(client: EgoiClient) -> tuple[list[dict], Optional[int]]:
                tag_index = (
                    await _tags.get_tag_index(client) if resolve_tags_flag else None
                )

                def _normalise(item: Any) -> dict:
                    return Q.normalize_contact(item, tag_index=tag_index)

                if segment_id is not None:
                    segment_id_value = segment_id

                    async def page(offset: int, limit: int):
                        return await client.get_all_contacts_by_segment(
                            list_id=list_id,
                            segment_id=segment_id_value,
                            offset=offset,
                            limit=limit,
                        )
                else:
                    async def page(offset: int, limit: int):
                        kwargs: dict[str, Any] = {
                            "list_id": list_id,
                            "offset": offset,
                            "limit": limit,
                        }
                        if status is not None:
                            kwargs["status"] = status
                        return await client.get_all_contacts(**kwargs)

                rows, server_total = await Q.iter_all_pages(
                    page,
                    max_rows=max_rows,
                    normaliser=_normalise,
                )
                # Tag the list_id on every row for downstream clarity.
                for r in rows:
                    r.setdefault("list_id", list_id)
                return rows, server_total

            export_csv_flag = bool(params.get("export_csv", False))

            # Streaming path: very large enumerations would OOM the worker
            # if we accumulated every row + materialised the full CSV in
            # memory. Above STREAM_THRESHOLD, persist rows to a tempfile
            # CSV one page at a time.
            STREAM_THRESHOLD = 5000
            use_streaming = export_csv_flag and max_rows >= STREAM_THRESHOLD

            if use_streaming:
                async def _stream_list(client: EgoiClient):
                    tag_index = (
                        await _tags.get_tag_index(client)
                        if resolve_tags_flag
                        else None
                    )

                    def _normalise(item: Any) -> dict:
                        return Q.normalize_contact(item, tag_index=tag_index)

                    if segment_id is not None:
                        segment_id_value = segment_id

                        async def page(offset: int, limit: int):
                            return await client.get_all_contacts_by_segment(
                                list_id=list_id,
                                segment_id=segment_id_value,
                                offset=offset,
                                limit=limit,
                            )
                    else:
                        async def page(offset: int, limit: int):
                            kwargs: dict[str, Any] = {
                                "list_id": list_id,
                                "offset": offset,
                                "limit": limit,
                            }
                            if status is not None:
                                kwargs["status"] = status
                            return await client.get_all_contacts(**kwargs)

                    async for row, total in Q.iter_all_pages_stream(
                        page,
                        max_rows=max_rows,
                        normaliser=_normalise,
                    ):
                        row.setdefault("list_id", list_id)
                        yield row, total

                return await _run.run_search_streaming(
                    self,
                    agent_context=agent_context,
                    config=config,
                    streamer=_stream_list,
                    filename_prefix="egoi_contact_search_list",
                    csv_columns=[
                        "contact_id",
                        "list_id",
                        "status",
                        "email",
                        "first_name",
                        "last_name",
                        "cellphone",
                        "telephone",
                        "birth_date",
                        "language",
                        "created",
                        "updated",
                        "tags",
                        "extra",
                    ],
                    summary_group_by=["status", "language"],
                    extra_data={
                        "scope": "list",
                        "list_id": list_id,
                        "segment_id": segment_id,
                        "status": status,
                    },
                    operation_label="egoi contact_search list (stream)",
                )

            return await _run.run_search(
                self,
                agent_context=agent_context,
                config=config,
                fetcher=_fetch_list,
                filename_prefix="egoi_contact_search_list",
                export_csv=export_csv_flag,
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
