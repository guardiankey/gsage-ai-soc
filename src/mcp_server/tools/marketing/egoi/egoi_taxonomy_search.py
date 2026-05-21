"""gSage AI — E-goi tag/segment taxonomy search tool.

Lists tag and segment definitions from the configured E-goi account.
The taxonomy is the "vocabulary" used to filter, segment and target
contacts; agents need it before composing any contact_search /
contact_manage request that references tag or segment names.

Permission: ``egoi:read``

Two ``kind`` modes are supported:

* ``tags`` — paginate ``GET /tags`` and return ``{tag_id, name, color}``.
  Counts (``contacts_count``) are **not** supported because the upstream
  API does not expose a per-tag contact count; agents that need them
  must use :class:`EgoiContactSearchTool` with ``tag_id``/``tag_name``
  (which forces background execution and returns CSV artifacts).
* ``segments`` — paginate ``GET /lists/{list_id}/segments`` and return
  ``{segment_id, name, type, contacts_count, created, updated}``.
  ``include_counts`` triggers one extra ``GET /lists/{id}/segments/
  {sid}/contacts?limit=1`` per row to fill ``contacts_count`` when the
  upstream payload omits it.
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

# Concurrency cap for the ``include_counts`` fan-out. Each lookup is one
# upstream API call; the E-goi public limit is 30 req/min per tool, but
# bursts within that window are fine — 8 keeps tail latency reasonable
# without tripping the per-tool circuit breaker.
_COUNT_CONCURRENCY = 8

# Row-count breakpoint above which an ``include_counts`` request is
# auto-routed to a Celery worker.
_BACKGROUND_COUNT_THRESHOLD = 100


class EgoiTaxonomySearchTool(BaseTool):
    """Search the tag/segment taxonomy of the configured E-goi account.

    Returns one row per tag or segment. Use this *before* any tag- or
    segment-filtered contact action to discover canonical ids and names.

    Permission: ``egoi:read``
    """

    name: ClassVar[str] = "egoi_taxonomy_search"
    config_namespace: ClassVar[str] = "egoi"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "List E-goi tags or segments. Use this before any tag/segment-"
        "filtered contact operation to discover ids and names. Set "
        "kind='tags' for account-wide tags or kind='segments' (with "
        "list_id) for per-list segments."
    )
    category: ClassVar[str] = "marketing"
    permissions: ClassVar[list[str]] = ["egoi:read"]

    rate_limit_per_minute: ClassVar[int] = 30
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

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["tags", "segments"],
                "description": (
                    "Taxonomy domain. 'tags' is account-wide; 'segments' "
                    "is scoped to a single list (requires 'list_id')."
                ),
            },
            "list_id": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "List id whose segments to enumerate. Required when "
                    "kind='segments'; ignored when kind='tags'."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Optional case-insensitive substring filter on the "
                    "tag/segment name."
                ),
            },
            "segment_type": {
                "type": "string",
                "enum": ["auto", "saved", "tag"],
                "description": (
                    "Filter segments by their backing type. Only used "
                    "when kind='segments'."
                ),
            },
            "include_counts": {
                "type": "boolean",
                "default": False,
                "description": (
                    "When kind='segments', fetch the contacts_count for "
                    "each segment if not present in the listing payload "
                    "(one extra API call per segment, capped at "
                    f"{_COUNT_CONCURRENCY} concurrent requests). Not "
                    "supported for kind='tags' (upstream API has no "
                    "per-tag count); use egoi_contact_search to "
                    "enumerate tagged contacts instead."
                ),
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
                    "Persist all rows as a CSV file artifact (forced "
                    "when the result set exceeds the inline preview)."
                ),
            },
        },
        "required": ["kind"],
        "additionalProperties": False,
    }

    async def should_run_background(self, params: dict, config: dict) -> bool:
        # An ``include_counts`` request on a big taxonomy fans out many
        # extra calls; route to Celery early.
        if bool(params.get("include_counts")) and _run.should_background_for_size(
            params,
            rows_threshold=_BACKGROUND_COUNT_THRESHOLD,
            export_rows_threshold=_BACKGROUND_COUNT_THRESHOLD,
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
        kind = (params.get("kind") or "").strip().lower()
        if kind not in {"tags", "segments"}:
            return self._failure(
                "VALIDATION_ERROR",
                "'kind' must be 'tags' or 'segments'",
            )
        list_id_raw = params.get("list_id")
        list_id: Optional[int] = (
            int(list_id_raw)
            if isinstance(list_id_raw, int) and list_id_raw > 0
            else None
        )
        if kind == "segments" and list_id is None:
            return self._failure(
                "VALIDATION_ERROR",
                "kind='segments' requires a positive integer 'list_id'",
            )

        name_filter = (params.get("name") or "").strip().lower()
        max_rows = Q.clamp_max_rows(params.get("max_rows"))
        include_counts = bool(params.get("include_counts", False))
        segment_type = (params.get("segment_type") or "").strip().lower() or None

        if kind == "tags" and include_counts:
            return self._failure(
                "VALIDATION_ERROR",
                "include_counts is not supported for kind='tags'; the "
                "E-goi API exposes no per-tag contact count. Use "
                "egoi_contact_search with tag_id/tag_name to enumerate "
                "tagged contacts (returns CSV).",
            )

        if kind == "tags":
            return await self._run_tags(
                agent_context=agent_context,
                config=config,
                name_filter=name_filter,
                max_rows=max_rows,
                export_csv=bool(params.get("export_csv", False)),
            )

        assert list_id is not None  # narrowed above
        return await self._run_segments(
            agent_context=agent_context,
            config=config,
            list_id=list_id,
            name_filter=name_filter,
            segment_type=segment_type,
            include_counts=include_counts,
            max_rows=max_rows,
            export_csv=bool(params.get("export_csv", False)),
        )

    # ── kind=tags ─────────────────────────────────────────────────────

    async def _run_tags(
        self,
        *,
        agent_context: AgentContext,
        config: dict,
        name_filter: str,
        max_rows: int,
        export_csv: bool,
    ) -> ToolResult:
        def _match(row: dict) -> bool:
            if not name_filter:
                return True
            return name_filter in str(row.get("name") or "").lower()

        def _normalise(item: Any) -> dict:
            if not isinstance(item, dict):
                return {}
            return {
                "tag_id": item.get("tag_id"),
                "name": item.get("name"),
                "color": item.get("color"),
            }

        async def _fetch(client: EgoiClient) -> tuple[list[dict], Optional[int]]:
            async def page(offset: int, limit: int):
                return await client.get_all_tags(offset=offset, limit=limit)

            rows, server_total = await Q.iter_all_pages(
                page, max_rows=max_rows, normaliser=_normalise
            )
            rows = [r for r in rows if r and _match(r)]
            return rows, server_total

        return await _run.run_search(
            self,
            agent_context=agent_context,
            config=config,
            fetcher=_fetch,
            filename_prefix="egoi_taxonomy_search_tags",
            export_csv=export_csv,
            summary_group_by=None,
            extra_data={"kind": "tags"},
            operation_label="egoi taxonomy_search tags",
        )

    # ── kind=segments ─────────────────────────────────────────────────

    async def _run_segments(
        self,
        *,
        agent_context: AgentContext,
        config: dict,
        list_id: int,
        name_filter: str,
        segment_type: Optional[str],
        include_counts: bool,
        max_rows: int,
        export_csv: bool,
    ) -> ToolResult:
        def _match(row: dict) -> bool:
            if name_filter and name_filter not in str(row.get("name") or "").lower():
                return False
            if segment_type and str(row.get("type") or "").lower() != segment_type:
                return False
            return True

        async def _fetch(client: EgoiClient) -> tuple[list[dict], Optional[int]]:
            async def page(offset: int, limit: int):
                return await client.get_all_segments(
                    list_id=list_id, offset=offset, limit=limit
                )

            rows, server_total = await Q.iter_all_pages(
                page, max_rows=max_rows, normaliser=Q.normalize_segment
            )
            # Annotate list_id on every row for downstream clarity.
            for r in rows:
                r.setdefault("list_id", list_id)
            rows = [r for r in rows if _match(r)]

            if include_counts and rows:
                await self._populate_segment_counts(client, list_id, rows)
            return rows, server_total

        return await _run.run_search(
            self,
            agent_context=agent_context,
            config=config,
            fetcher=_fetch,
            filename_prefix=f"egoi_taxonomy_search_segments_list_{list_id}",
            export_csv=export_csv,
            summary_group_by=["type"],
            extra_data={"kind": "segments", "list_id": list_id},
            operation_label="egoi taxonomy_search segments",
        )

    @staticmethod
    async def _populate_segment_counts(
        client: EgoiClient, list_id: int, rows: list[dict]
    ) -> None:
        """Fill missing ``contacts_count`` by probing each segment.

        ``GET /lists/{id}/segments/{sid}/contacts?limit=1`` returns
        ``total_items`` which is the authoritative count. We skip rows
        where ``contacts_count`` is already present in the listing
        payload to save API quota.
        """
        sem = asyncio.Semaphore(_COUNT_CONCURRENCY)

        async def _probe(row: dict) -> None:
            if row.get("contacts_count") is not None:
                return
            sid_raw = row.get("segment_id")
            try:
                sid = int(sid_raw) if sid_raw is not None else None
            except (TypeError, ValueError):
                sid = None
            if sid is None:
                return
            async with sem:
                try:
                    payload = await client.get_all_contacts_by_segment(
                        list_id=list_id, segment_id=sid, offset=0, limit=1
                    )
                except Exception:  # noqa: BLE001 — keep partial data
                    log.debug(
                        "taxonomy_search: count probe failed for segment %s", sid
                    )
                    return
            row["contacts_count"] = Q.total_items(payload)

        await asyncio.gather(*(_probe(r) for r in rows))
