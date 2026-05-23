"""gSage AI — Curator reputation lists (read) tool.

Read-only access to reputation list collections managed by the Curator
microservice (internal Docker service).

Supported actions:
    list_collections — List all reputation list collections (with optional filter)
    view_items       — Query items inside a specific collection (paginated)

Required permission: ``curator:read``
No approval required.
"""

from __future__ import annotations

import logging
import math
import time
from typing import ClassVar, Optional

import httpx

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.result_export import build_agent_payload, summarize
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# Defensive cap on rows materialised for CSV export, mirroring the pattern
# used by cisa_kev / msrc_bulletin / GLPI tools. A single Curator collection
# can grow large (millions of IPs in extreme cases) so we cap aggressively.
_CSV_FETCH_LIMIT: int = 10_000

_CURATOR_CONFIG_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "base_url": {
            "type": "string",
            "description": "Curator service base URL (default: http://curator:8000).",
        },
        "api_key": {
            "type": "string",
            "description": "Curator admin API key (X-API-Key header).",
            "sensitive": True,
        },
    },
    "additionalProperties": False,
}
_CURATOR_CONFIG_DEFAULTS: dict = {
    "base_url": "http://curator:8000",
}


class CuratorListsTool(BaseTool):
    """Read reputation list data from the Curator microservice.

    **Actions:**

    - ``list_collections`` — List all reputation list collections (IP blocklists,
      domain lists, hash lists, etc.). Supports ``active_only`` and
      ``published_only`` filters. Each returned collection includes the
      ``published`` boolean so the agent knows whether the list is exposed
      via the public /data/ HTTP endpoints or kept private to the admin API.

    - ``view_items`` — Query individual items inside a specific collection.
      Supports filtering by value, item type (blocklist/allowlist/suspected),
      and pagination.

    For **write operations** (add items, delete items, create/update
    collections), use ``curator_manage``.

    **Bulk / CSV output**

    For ``view_items``:

    - ``per_page`` controls only the inline preview shipped to the agent
      (default 50, max 500).
    - Set ``export_csv=true`` to receive every matching row as a CSV
      artifact (capped defensively at 10 000 rows). The tool paginates
      the Curator API internally to collect the full result set.
    - A CSV is also produced automatically whenever the total number of
      matching items exceeds ``per_page`` (overflow), so the agent always
      has a downloadable file when the inline preview is incomplete.
    - ``group_by`` + ``top_n`` drive the aggregated summary returned
      alongside the rows (defaults to top values by ``type``).
    - Date-range filters are applied server-side: ``created_from`` /
      ``created_to`` and ``expire_from`` / ``expire_to`` accept ISO 8601
      (with TZ) or ``YYYY-MM-DD``. Shortcuts ``created_within_days`` /
      ``expires_within_days`` are also available, plus the flags
      ``never_expires`` and ``expired_only`` for IOC hygiene queries
      (e.g. "all entries already expired" or "permanent allow-list").

    Permission: ``curator:read``
    """

    name: ClassVar[str] = "curator_lists"
    config_namespace: ClassVar[str] = "curator"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Read reputation list data (IPs, domains, hashes) from the Curator microservice"
    category: ClassVar[str] = "threat_intel"
    permissions: ClassVar[list[str]] = ["curator:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False

    audit_output: ClassVar[bool] = True

    config_schema: ClassVar[Optional[dict]] = _CURATOR_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = _CURATOR_CONFIG_DEFAULTS
    requires_config: ClassVar[bool] = False

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_collections", "view_items"],
                "description": (
                    "Operation to perform:\n"
                    "- list_collections: list all reputation list collections\n"
                    "- view_items: query items inside a specific collection (requires collection_id)"
                ),
            },
            # ── list_collections params ──────────────────────────────────────
            "active_only": {
                "type": "boolean",
                "description": (
                    "Used with list_collections. "
                    "If true, return only active collections (default: false)."
                ),
            },            "published_only": {
                "type": "boolean",
                "description": (
                    "Used with list_collections. If true, return only "
                    "published collections (those exposed via the public "
                    "/data/ HTTP endpoints; default: false). When omitted "
                    "or false, both published and private collections are "
                    "returned (the 'published' flag is included in each "
                    "collection object so the agent can tell them apart)."
                ),
            },            # ── view_items params ────────────────────────────────────────────
            "collection_id": {
                "type": "integer",
                "minimum": 1,
                "description": "Collection ID. Required for view_items.",
            },
            "value": {
                "type": "string",
                "description": (
                    "Used with view_items. "
                    "Filter by exact value (IP address, domain, hash, etc.)."
                ),
            },
            "item_type": {
                "type": "string",
                "enum": ["blocklist", "allowlist", "suspected"],
                "description": (
                    "Used with view_items. "
                    "Filter by item type: blocklist, allowlist, or suspected."
                ),
            },
            # ── Date-range filters (view_items) ──────────────────────────────────
            # All date inputs accept either ISO 8601 (e.g. 2025-01-15T00:00:00Z)
            # or YYYY-MM-DD (treated as UTC midnight). Server-side filtering.
            "created_from": {
                "type": "string",
                "description": (
                    "Used with view_items. Lower bound (inclusive) for the "
                    "item's created_at. Accepts ISO 8601 with TZ or YYYY-MM-DD "
                    "(interpreted as UTC)."
                ),
            },
            "created_to": {
                "type": "string",
                "description": (
                    "Used with view_items. Upper bound (inclusive) for the "
                    "item's created_at. Same format as created_from."
                ),
            },
            "expire_from": {
                "type": "string",
                "description": (
                    "Used with view_items. Lower bound (inclusive) for the "
                    "item's expire_at. Excludes never-expiring items unless "
                    "never_expires=true is also set."
                ),
            },
            "expire_to": {
                "type": "string",
                "description": (
                    "Used with view_items. Upper bound (inclusive) for the "
                    "item's expire_at. Excludes never-expiring items unless "
                    "never_expires=true is also set."
                ),
            },
            "created_within_days": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Used with view_items. Shortcut: keep only items created "
                    "within the last N days from now (UTC). ANDed with "
                    "created_from/to if both supplied."
                ),
            },
            "expires_within_days": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Used with view_items. Shortcut: keep items that will "
                    "expire within the next N days. Implicitly excludes "
                    "never-expiring entries."
                ),
            },
            "never_expires": {
                "type": "boolean",
                "description": (
                    "Used with view_items. If true, return only items with "
                    "no expiry (permanent). If false, exclude them. If "
                    "omitted, no filter on this dimension."
                ),
            },
            "expired_only": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Used with view_items. If true, return only items whose "
                    "expire_at is already in the past (expired but not yet "
                    "pruned by the cleanup task)."
                ),
            },
            "page": {
                "type": "integer",
                "minimum": 1,
                "default": 1,
                "description": (
                    "Page number for the inline preview (default: 1). "
                    "Ignored when export_csv=true or when the total result "
                    "set exceeds per_page (the tool paginates internally)."
                ),
            },
            "per_page": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "default": 50,
                "description": (
                    "Items per page in the inline preview only "
                    "(default: 50, max: 500). The CSV artifact always "
                    "contains the full filtered result, capped at "
                    "10 000 rows defensively."
                ),
            },
            "export_csv": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Used with view_items. When true, fetch all matching "
                    "items (up to 10 000) and ship them as a downloadable "
                    "CSV artifact in addition to the inline preview. "
                    "A CSV is also produced automatically whenever the "
                    "total exceeds per_page."
                ),
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Used with view_items. Columns to aggregate in the "
                    "top-N summary. Defaults to ['type'] when omitted. "
                    "Common values: type, public_reference, reference."
                ),
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 10,
                "description": (
                    "Number of top values per group_by column in the "
                    "summary (default: 10)."
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
        action = params["action"]

        base_url = (config.get("base_url") or _CURATOR_CONFIG_DEFAULTS["base_url"]).rstrip("/")
        api_key = config.get("api_key") or ""
        headers = {"X-API-Key": api_key}

        try:
            async with httpx.AsyncClient(
                base_url=base_url,
                headers=headers,
                timeout=self.timeout_seconds,
            ) as client:
                if action == "list_collections":
                    result = await self._list_collections(client, params)
                elif action == "view_items":
                    result = await self._view_items(client, params, agent_context)
                else:
                    return self._failure("INVALID_ACTION", f"Unknown action: {action}")

        except httpx.TimeoutException:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("TIMEOUT", "Request to Curator timed out", retryable=True, execution_time_ms=elapsed)
        except httpx.HTTPStatusError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.response.status_code in (429, 500, 502, 503, 504)
            return self._failure(
                f"HTTP_{exc.response.status_code}",
                f"Curator API error {exc.response.status_code}: {exc.response.text}",
                retryable=retryable,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("curator_lists: unexpected error (action=%s)", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(result, execution_time_ms=elapsed)

    # ── Action handlers ────────────────────────────────────────────────────

    async def _list_collections(self, client: httpx.AsyncClient, params: dict) -> dict:
        """List all reputation list collections."""
        active_only = params.get("active_only", False)
        published_only = params.get("published_only", False)
        resp = await client.get(
            "/a/list_collections",
            params={
                "active_only": str(active_only).lower(),
                "published_only": str(published_only).lower(),
            },
        )
        resp.raise_for_status()
        collections = resp.json()
        return {
            "action": "list_collections",
            "count": len(collections),
            "collections": collections,
        }

    async def _view_items(
        self,
        client: httpx.AsyncClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        """Query items inside a specific collection.

        Always fetches a first page to learn ``total``. When the caller asked
        for CSV export or when the total exceeds ``per_page`` (the inline
        preview cannot show everything), all remaining pages are pulled
        sequentially up to ``_CSV_FETCH_LIMIT`` rows so the full filtered
        result can be shipped as a CSV artifact.
        """
        collection_id = params.get("collection_id")
        if not collection_id:
            raise ValueError("collection_id is required for action=view_items")

        per_page = int(params.get("per_page", 50))
        start_page = int(params.get("page", 1))
        export_csv: bool = bool(params.get("export_csv", False))
        group_by_param = params.get("group_by") or None
        top_n: int = int(params.get("top_n", 10))

        base_query: dict = {"per_page": per_page}
        if params.get("value"):
            base_query["value"] = params["value"]
        if params.get("item_type"):
            base_query["type"] = params["item_type"]
        # Date-range / expiry filters — forwarded straight to the Curator API
        # which validates the format server-side.
        for date_key in (
            "created_from",
            "created_to",
            "expire_from",
            "expire_to",
        ):
            if params.get(date_key):
                base_query[date_key] = params[date_key]
        for int_key in ("created_within_days", "expires_within_days"):
            if params.get(int_key) is not None:
                base_query[int_key] = int(params[int_key])
        if params.get("never_expires") is not None:
            base_query["never_expires"] = str(bool(params["never_expires"])).lower()
        if params.get("expired_only"):
            base_query["expired_only"] = "true"

        # ── First page ────────────────────────────────────────────────────
        first_query = {**base_query, "page": start_page}
        resp = await client.get(f"/a/{collection_id}/view_item", params=first_query)
        resp.raise_for_status()
        first_payload = resp.json()
        total = int(first_payload.get("total", 0))
        items: list[dict] = list(first_payload.get("items", []))

        # Decide whether to paginate the rest of the result set. We paginate
        # when CSV was explicitly requested OR when the total overflows what
        # the inline preview can hold (so the agent always gets a CSV with
        # the full data when the preview is incomplete).
        overflow = total > len(items) + ((start_page - 1) * per_page)
        need_full = export_csv or overflow
        csv_truncated = False

        if need_full and total > 0:
            # Always (re)start from page 1 when building the full export to
            # avoid gaps from a caller-supplied non-1 start_page.
            if start_page != 1:
                items = []
                first_full = await client.get(
                    f"/a/{collection_id}/view_item",
                    params={**base_query, "page": 1},
                )
                first_full.raise_for_status()
                items = list(first_full.json().get("items", []))

            total_pages = math.ceil(total / per_page) if per_page > 0 else 1
            page = 2
            while page <= total_pages and len(items) < _CSV_FETCH_LIMIT:
                page_resp = await client.get(
                    f"/a/{collection_id}/view_item",
                    params={**base_query, "page": page},
                )
                page_resp.raise_for_status()
                items.extend(page_resp.json().get("items", []))
                page += 1

            if len(items) >= _CSV_FETCH_LIMIT and total > _CSV_FETCH_LIMIT:
                csv_truncated = True
                items = items[:_CSV_FETCH_LIMIT]

        # ── Agent payload (inline preview + optional CSV artifact) ────────
        agent_payload = await build_agent_payload(
            tool=self,
            rows=items,
            export_csv=export_csv,
            export_json=False,
            filename_prefix=f"{self.name}_view",
            agent_context=agent_context,
            preview_rows=per_page,
        )

        # ── Top-N summary (over the materialised result set) ──────────────
        summary_group_by = group_by_param or ["type"]
        agg_summary = summarize(
            items,
            group_by=summary_group_by,
            top_n=top_n,
            sample_size=0,
        )

        filters_applied: dict = {}
        if params.get("value"):
            filters_applied["value"] = params["value"]
        if params.get("item_type"):
            filters_applied["item_type"] = params["item_type"]
        for fkey in (
            "created_from",
            "created_to",
            "expire_from",
            "expire_to",
            "created_within_days",
            "expires_within_days",
            "never_expires",
            "expired_only",
        ):
            if params.get(fkey) not in (None, False, ""):
                filters_applied[fkey] = params[fkey]
        if export_csv:
            filters_applied["export_csv"] = True

        return {
            "action": "view_items",
            "collection_id": collection_id,
            "summary": {
                "total": total,
                "returned_count": len(agent_payload["rows_preview"]),
                "materialised_count": len(items),
                "csv_truncated": csv_truncated,
                "csv_row_limit": _CSV_FETCH_LIMIT,
                "filters_applied": filters_applied,
                "aggregations": agg_summary,
            },
            "page": start_page if not need_full else 1,
            "per_page": per_page,
            "rows_total": agent_payload["rows_total"],
            "rows_overflow": agent_payload["rows_overflow"],
            "rows_preview_limit": per_page,
            "artifacts": agent_payload["artifacts"],
            "agent_hint": agent_payload["agent_hint"],
            "items": agent_payload["rows_preview"],
        }
