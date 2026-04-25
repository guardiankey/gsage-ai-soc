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
import time
from typing import ClassVar, Optional

import httpx

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

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
      domain lists, hash lists, etc.). Supports ``active_only`` filter.

    - ``view_items`` — Query individual items inside a specific collection.
      Supports filtering by value, item type (blocklist/allowlist/suspected),
      and pagination.

    For **write operations** (add items, delete items, create/update
    collections), use ``curator_manage``.

    Permission: ``curator:read``
    """

    name: ClassVar[str] = "curator_lists"
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
            },
            # ── view_items params ────────────────────────────────────────────
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
            "page": {
                "type": "integer",
                "minimum": 1,
                "default": 1,
                "description": "Page number (default: 1).",
            },
            "per_page": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "default": 50,
                "description": "Items per page (default: 50, max: 500).",
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
                    result = await self._view_items(client, params)
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
        resp = await client.get("/a/list_collections", params={"active_only": str(active_only).lower()})
        resp.raise_for_status()
        collections = resp.json()
        return {
            "action": "list_collections",
            "count": len(collections),
            "collections": collections,
        }

    async def _view_items(self, client: httpx.AsyncClient, params: dict) -> dict:
        """Query items inside a specific collection."""
        collection_id = params.get("collection_id")
        if not collection_id:
            raise ValueError("collection_id is required for action=view_items")

        query: dict = {
            "page": params.get("page", 1),
            "per_page": params.get("per_page", 50),
        }
        if params.get("value"):
            query["value"] = params["value"]
        if params.get("item_type"):
            query["type"] = params["item_type"]

        resp = await client.get(f"/a/{collection_id}/view_item", params=query)
        resp.raise_for_status()
        return {"action": "view_items", "collection_id": collection_id, **resp.json()}
