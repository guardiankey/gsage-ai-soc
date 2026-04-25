"""gSage AI — Curator reputation lists (write) tool.

Write operations for the Curator reputation list management microservice.
All actions require ``curator:write`` permission and human-in-the-loop approval.

Supported actions:
    add_items          — Add one or more items to a collection (upserts duplicates)
    del_item           — Remove an item from a collection
    create_collection  — Create a new reputation list collection
    update_collection  — Update metadata of an existing collection

Required permission: ``curator:write``
All actions require human approval.
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


class CuratorManageTool(BaseTool):
    """Add and remove entries from Curator reputation lists, and manage collections.

    All actions are write operations that modify the Curator service and
    require ``curator:write`` permission plus human-in-the-loop approval.

    **Actions:**

    - ``add_items`` — Add one or more entries to a reputation list collection.
      Accepts ``items`` (array). Each item needs ``value`` and ``item_type``
      (``blocklist`` | ``allowlist`` | ``suspected``). Optionally ``public_reference``,
      ``reference``, and ``expire_days``. Duplicates are upserted (dates updated).
      Triggers an async file dump of the collection after writing.

    - ``del_item`` — Remove a single entry from a collection by value and item_type.
      Triggers an async file dump after removal.

    - ``create_collection`` — Create a new reputation list collection.
      Requires ``short_description`` and ``collection_type``. Optionally ``subtype``,
      ``description``, ``active``.
      Collection types: ip, cidr, domain, url, domain_regex, file_hash_md5,
      file_hash_sha1, file_hash_sha256, email, asn, ja3, ja4.

    - ``update_collection`` — Update the description or active status of an existing
      collection. Requires ``collection_id``.

    Permission: ``curator:write``
    """

    name: ClassVar[str] = "curator_manage"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Add, remove, and manage entries in Curator reputation lists and collections"
    category: ClassVar[str] = "threat_intel"
    permissions: ClassVar[list[str]] = ["curator:write"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 30
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True

    audit_output: ClassVar[bool] = True

    config_schema: ClassVar[Optional[dict]] = _CURATOR_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = _CURATOR_CONFIG_DEFAULTS
    requires_config: ClassVar[bool] = False

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action", "_approval_summary"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add_items", "del_item", "create_collection", "update_collection"],
                "description": (
                    "Operation to perform:\n"
                    "- add_items: add one or more entries to a collection (requires collection_id, items)\n"
                    "- del_item: remove an entry from a collection (requires collection_id, value, item_type)\n"
                    "- create_collection: create a new reputation list (requires short_description, collection_type)\n"
                    "- update_collection: update collection metadata (requires collection_id)"
                ),
            },
            "_approval_summary": {
                "type": "string",
                "description": (
                    "Human-readable summary of what this operation will do, shown to the approver. "
                    "Be specific: include collection name/ID, values affected, and reason."
                ),
            },
            # ── add_items / del_item shared params ───────────────────────────
            "collection_id": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Target collection ID. Required for add_items, del_item, and update_collection. "
                    "Use curator_lists action=list_collections to discover IDs."
                ),
            },
            # ── add_items params ─────────────────────────────────────────────
            "items": {
                "type": "array",
                "description": (
                    "List of items to add. Required for add_items. "
                    "Each item must have 'value' and 'item_type'. "
                    "Optionally 'public_reference', 'reference', 'expire_days'."
                ),
                "items": {
                    "type": "object",
                    "required": ["value", "item_type"],
                    "properties": {
                        "value": {
                            "type": "string",
                            "description": "The entry value (IP, domain, hash, email, etc.).",
                        },
                        "item_type": {
                            "type": "string",
                            "enum": ["blocklist", "allowlist", "suspected"],
                            "description": "List type for this item.",
                        },
                        "public_reference": {
                            "type": "string",
                            "description": "Public source reference (e.g. CVE-ID, threat name, URL).",
                        },
                        "reference": {
                            "type": "string",
                            "description": "Internal reference (ticket ID, case number, etc.).",
                        },
                        "expire_days": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Days until this entry expires. Omit for permanent.",
                        },
                    },
                    "additionalProperties": False,
                },
                "minItems": 1,
            },
            # ── del_item params ──────────────────────────────────────────────
            "value": {
                "type": "string",
                "description": "Entry value to remove. Required for del_item.",
            },
            "item_type": {
                "type": "string",
                "enum": ["blocklist", "allowlist", "suspected"],
                "description": "Item type to remove. Required for del_item.",
            },
            # ── create_collection params ─────────────────────────────────────
            "short_description": {
                "type": "string",
                "description": (
                    "Short label for the collection (max 100 chars). "
                    "Required for create_collection. Together with subtype and collection_type "
                    "this forms the auto-generated slug."
                ),
            },
            "collection_type": {
                "type": "string",
                "description": (
                    "Type of entries this collection holds. Required for create_collection. "
                    "Valid values: ip, cidr, domain, url, domain_regex, "
                    "file_hash_md5, file_hash_sha1, file_hash_sha256, email, asn, ja3, ja4."
                ),
            },
            "subtype": {
                "type": "string",
                "description": (
                    "Optional subtype label (max 20 chars). Used for create_collection. "
                    "Example: 'smtp_servers', 'exit_nodes', 'tor'. "
                    "Included in slug generation."
                ),
            },
            "description": {
                "type": "string",
                "description": "Detailed description of the collection. Used for create/update_collection.",
            },
            "active": {
                "type": "boolean",
                "description": (
                    "Whether the collection is active (default: true). "
                    "Inactive collections are not dumped. Used for create/update_collection."
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

        if not agent_context.has_permission("curator:write"):
            return self._failure(
                "PERMISSION_DENIED",
                "This tool requires the 'curator:write' permission.",
            )

        base_url = (config.get("base_url") or _CURATOR_CONFIG_DEFAULTS["base_url"]).rstrip("/")
        api_key = config.get("api_key") or ""
        headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(
                base_url=base_url,
                headers=headers,
                timeout=self.timeout_seconds,
            ) as client:
                if action == "add_items":
                    result = await self._add_items(client, params)
                elif action == "del_item":
                    result = await self._del_item(client, params)
                elif action == "create_collection":
                    result = await self._create_collection(client, params)
                elif action == "update_collection":
                    result = await self._update_collection(client, params)
                else:
                    return self._failure("INVALID_ACTION", f"Unknown action: {action}")

        except httpx.TimeoutException:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("TIMEOUT", "Request to Curator timed out", retryable=True, execution_time_ms=elapsed)
        except httpx.HTTPStatusError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.response.status_code in (429, 500, 502, 503, 504)
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except Exception:
                detail = exc.response.text
            return self._failure(
                f"HTTP_{exc.response.status_code}",
                f"Curator API error {exc.response.status_code}: {detail}",
                retryable=retryable,
                execution_time_ms=elapsed,
            )
        except ValueError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INVALID_PARAMS", str(exc), execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("curator_manage: unexpected error (action=%s)", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(result, execution_time_ms=elapsed)

    # ── Action handlers ────────────────────────────────────────────────────

    async def _add_items(self, client: httpx.AsyncClient, params: dict) -> dict:
        """Add one or more items to a collection (upserts on duplicate)."""
        collection_id = params.get("collection_id")
        if not collection_id:
            raise ValueError("collection_id is required for action=add_items")

        items_raw = params.get("items")
        if not items_raw:
            raise ValueError("items array is required for action=add_items")

        added: list[dict] = []
        failed: list[dict] = []

        for entry in items_raw:
            payload = {
                "value": entry["value"],
                "type": entry["item_type"],
            }
            if entry.get("public_reference"):
                payload["public_reference"] = entry["public_reference"]
            if entry.get("reference"):
                payload["reference"] = entry["reference"]
            if entry.get("expire_days") is not None:
                payload["expire_days"] = entry["expire_days"]

            try:
                resp = await client.post(f"/a/{collection_id}/add_item", json=payload)
                resp.raise_for_status()
                added.append({"value": entry["value"], "item_type": entry["item_type"]})
            except httpx.HTTPStatusError as exc:
                try:
                    detail = exc.response.json().get("detail", exc.response.text)
                except Exception:
                    detail = exc.response.text
                failed.append({
                    "value": entry["value"],
                    "item_type": entry["item_type"],
                    "error": f"HTTP {exc.response.status_code}: {detail}",
                })
                log.warning(
                    "curator_manage add_items: failed for value=%r collection=%s — %s",
                    entry["value"], collection_id, detail,
                )

        status = "partial" if failed and added else ("error" if failed else "success")
        result = {
            "action": "add_items",
            "collection_id": collection_id,
            "added": len(added),
            "failed": len(failed),
            "added_items": added,
        }
        if failed:
            result["errors"] = failed

        if status == "partial":
            return result  # caller can inspect — we still return via _success path
        return result

    async def _del_item(self, client: httpx.AsyncClient, params: dict) -> dict:
        """Remove an item from a collection by value and item_type."""
        collection_id = params.get("collection_id")
        value = params.get("value", "").strip()
        item_type = params.get("item_type", "").strip()

        if not collection_id:
            raise ValueError("collection_id is required for action=del_item")
        if not value:
            raise ValueError("value is required for action=del_item")
        if not item_type:
            raise ValueError("item_type is required for action=del_item")

        payload = {"value": value, "type": item_type}
        resp = await client.request("DELETE", f"/a/{collection_id}/del_item", json=payload)
        resp.raise_for_status()
        return {"action": "del_item", "collection_id": collection_id, **resp.json()}

    async def _create_collection(self, client: httpx.AsyncClient, params: dict) -> dict:
        """Create a new reputation list collection."""
        short_description = params.get("short_description", "").strip()
        collection_type = params.get("collection_type", "").strip()

        if not short_description:
            raise ValueError("short_description is required for action=create_collection")
        if not collection_type:
            raise ValueError("collection_type is required for action=create_collection")

        payload: dict = {
            "short_description": short_description,
            "type": collection_type,
        }
        if params.get("subtype"):
            payload["subtype"] = params["subtype"]
        if params.get("description") is not None:
            payload["description"] = params["description"]
        if params.get("active") is not None:
            payload["active"] = params["active"]

        resp = await client.post("/a/create_collection", json=payload)
        resp.raise_for_status()
        return {"action": "create_collection", **resp.json()}

    async def _update_collection(self, client: httpx.AsyncClient, params: dict) -> dict:
        """Update metadata of an existing collection."""
        collection_id = params.get("collection_id")
        if not collection_id:
            raise ValueError("collection_id is required for action=update_collection")

        payload: dict = {}
        if params.get("short_description") is not None:
            payload["short_description"] = params["short_description"]
        if params.get("description") is not None:
            payload["description"] = params["description"]
        if params.get("active") is not None:
            payload["active"] = params["active"]

        if not payload:
            raise ValueError(
                "At least one field must be provided for update_collection: "
                "short_description, description, or active."
            )

        resp = await client.put(f"/a/{collection_id}/update_collection", json=payload)
        resp.raise_for_status()
        return {"action": "update_collection", **resp.json()}
