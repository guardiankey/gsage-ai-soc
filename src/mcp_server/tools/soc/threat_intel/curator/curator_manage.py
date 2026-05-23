"""gSage AI — Curator reputation lists (write) tool.

Write operations for the Curator reputation list management microservice.
All actions require ``curator:write`` permission and human-in-the-loop approval.

Supported actions:
    add_items          — Add one or more items to a collection (upserts duplicates)
    import_csv         — Bulk add items from a stored CSV file (upserts duplicates)
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
from src.mcp_server.tools.core.csv.csv_loader import CSVAccessError, load_csv
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# Defensive cap on rows processed by a single import_csv call. Mirrors the
# pattern used by other CSV-aware tools (cisa_kev, msrc_bulletin,
# curator_lists). Imports above this limit must be split client-side.
_CSV_IMPORT_MAX_ROWS: int = 10_000

_VALID_ITEM_TYPES: tuple[str, ...] = ("blocklist", "allowlist", "suspected")

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

    - ``import_csv`` — Bulk-add entries from a previously uploaded CSV file.
      Requires ``file_id`` (UUID of a stored CSV). The CSV must use these
      fixed column names:

        * ``value`` (required) — the entry value (IP, domain, hash, …).
        * ``item_type`` (optional) — ``blocklist`` / ``allowlist`` / ``suspected``.
          Falls back to ``default_item_type`` when omitted.
        * ``public_reference`` (optional) — public source ref.
        * ``reference`` (optional) — internal ref / ticket.
        * ``expire_days`` (optional) — integer days until expiry.

      Per-column fallbacks may be supplied via ``default_item_type``,
      ``default_public_reference``, ``default_reference``, ``default_expire_days``.
      Set ``dry_run=true`` to validate the file (and see how many rows would
      be imported / rejected) without writing to the Curator service. Hard
      cap of 10 000 rows per call — split larger files client-side.
      Re-importing the same CSV is safe: the Curator service upserts and
      refreshes ``re_added_at``.

    - ``del_item`` — Remove a single entry from a collection by value and item_type.
      Triggers an async file dump after removal.

    - ``create_collection`` — Create a new reputation list collection.
      Requires ``short_description`` and ``collection_type``. Optionally ``subtype``,
      ``description``, ``active``, ``published``.
      Collection types: ip, cidr, domain, url, domain_regex, file_hash_md5,
      file_hash_sha1, file_hash_sha256, email, asn, ja3, ja4.

      Use ``published=false`` to create a collection that the agent can
      populate privately: it stays usable via the admin API but is hidden
      from the public /data/ HTTP listing and its dump files are not
      generated. Default ``published=true`` preserves legacy behaviour.

    - ``update_collection`` — Update metadata of an existing collection.
      Requires ``collection_id``. Any of ``short_description``, ``description``,
      ``active``, ``published`` can be updated. Toggling ``published`` controls
      whether the collection is exposed via the public /data/ HTTP endpoints
      (does not delete previously-dumped files on disk).

    Permission: ``curator:write``
    """

    name: ClassVar[str] = "curator_manage"
    config_namespace: ClassVar[str] = "curator"
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
                "enum": [
                    "add_items",
                    "import_csv",
                    "del_item",
                    "create_collection",
                    "update_collection",
                ],
                "description": (
                    "Operation to perform:\n"
                    "- add_items: add one or more entries to a collection (requires collection_id, items)\n"
                    "- import_csv: bulk-add entries from a stored CSV file (requires collection_id, file_id)\n"
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
            "published": {
                "type": "boolean",
                "description": (
                    "Whether the collection is exposed via the public /data/ HTTP "
                    "endpoints (default: true). When false, the collection is hidden "
                    "from public listings AND its dump files are not generated, but "
                    "the collection remains fully usable via the admin API "
                    "(curator_manage / curator_lists). Use 'published=false' for "
                    "agent-only / private lists. Used for create/update_collection."
                ),
            },
            # ── import_csv params ──────────────────────────────────────────
            "file_id": {
                "type": "string",
                "description": (
                    "UUID of the stored CSV file to import. Required for "
                    "action=import_csv. The CSV must have a header row with "
                    "the columns: value (required), item_type, public_reference, "
                    "reference, expire_days."
                ),
            },
            "default_item_type": {
                "type": "string",
                "enum": ["blocklist", "allowlist", "suspected"],
                "description": (
                    "Used with import_csv. Fallback item_type applied to "
                    "rows that have an empty/missing 'item_type' column."
                ),
            },
            "default_public_reference": {
                "type": "string",
                "description": (
                    "Used with import_csv. Fallback public_reference "
                    "applied to rows that omit it."
                ),
            },
            "default_reference": {
                "type": "string",
                "description": (
                    "Used with import_csv. Fallback reference applied to "
                    "rows that omit it."
                ),
            },
            "default_expire_days": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Used with import_csv. Fallback expire_days applied to "
                    "rows that omit it. Omit for permanent entries."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Used with import_csv. When true, validate the CSV and "
                    "return projected counts (would_add / invalid_rows) "
                    "without calling the Curator service."
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
                elif action == "import_csv":
                    result = await self._import_csv(client, params, agent_context)
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
        except CSVAccessError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                f"CSV_{exc.reason}", str(exc), execution_time_ms=elapsed
            )
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
            ok, err = await self._post_single_item(
                client,
                collection_id=int(collection_id),
                value=entry["value"],
                item_type=entry["item_type"],
                public_reference=entry.get("public_reference"),
                reference=entry.get("reference"),
                expire_days=entry.get("expire_days"),
            )
            if ok:
                added.append({"value": entry["value"], "item_type": entry["item_type"]})
            else:
                failed.append({
                    "value": entry["value"],
                    "item_type": entry["item_type"],
                    "error": err,
                })

        result = {
            "action": "add_items",
            "collection_id": collection_id,
            "added": len(added),
            "failed": len(failed),
            "added_items": added,
        }
        if failed:
            result["errors"] = failed
        return result

    async def _post_single_item(
        self,
        client: httpx.AsyncClient,
        *,
        collection_id: int,
        value: str,
        item_type: str,
        public_reference: Optional[str] = None,
        reference: Optional[str] = None,
        expire_days: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        """POST a single item to ``/a/{cid}/add_item`` with upsert semantics.

        Returns ``(ok, error_message)``. HTTP errors are caught and returned
        as a human-readable string so the caller can collect per-row outcomes
        without aborting the whole batch.
        """
        payload: dict = {"value": value, "type": item_type}
        if public_reference:
            payload["public_reference"] = public_reference
        if reference:
            payload["reference"] = reference
        if expire_days is not None:
            payload["expire_days"] = expire_days

        try:
            resp = await client.post(f"/a/{collection_id}/add_item", json=payload)
            resp.raise_for_status()
            return True, None
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except Exception:
                detail = exc.response.text
            err = f"HTTP {exc.response.status_code}: {detail}"
            log.warning(
                "curator_manage: add_item failed for value=%r collection=%s — %s",
                value, collection_id, detail,
            )
            return False, err

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
        if params.get("published") is not None:
            payload["published"] = params["published"]

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
        if params.get("published") is not None:
            payload["published"] = params["published"]

        if not payload:
            raise ValueError(
                "At least one field must be provided for update_collection: "
                "short_description, description, active, or published."
            )

        resp = await client.put(f"/a/{collection_id}/update_collection", json=payload)
        resp.raise_for_status()
        return {"action": "update_collection", **resp.json()}

    async def _import_csv(
        self,
        client: httpx.AsyncClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        """Bulk-add items to a collection from a previously stored CSV file.

        The CSV must use these column names (case-sensitive):

            value (required), item_type, public_reference, reference, expire_days

        Missing optional columns fall back to the matching ``default_*`` param
        (when supplied). Rows missing ``value`` or with an unresolved
        ``item_type`` are skipped and reported in ``errors``.

        Iterates ``POST /a/{cid}/add_item`` sequentially per row (upsert
        semantics; safe to re-run the same CSV).
        """
        collection_id = params.get("collection_id")
        if not collection_id:
            raise ValueError("collection_id is required for action=import_csv")

        file_id = params.get("file_id")
        if not file_id:
            raise ValueError("file_id is required for action=import_csv")

        default_item_type = params.get("default_item_type")
        default_public_reference = params.get("default_public_reference")
        default_reference = params.get("default_reference")
        default_expire_days = params.get("default_expire_days")
        dry_run: bool = bool(params.get("dry_run", False))

        # ── Load + parse CSV ──────────────────────────────────────────────────
        df, meta = await load_csv(self, agent_context, str(file_id))
        total_rows = int(meta.get("rows", df.height))

        if total_rows > _CSV_IMPORT_MAX_ROWS:
            raise ValueError(
                f"CSV has {total_rows} rows; the per-call cap is "
                f"{_CSV_IMPORT_MAX_ROWS}. Split the file client-side and "
                "issue multiple import_csv calls."
            )

        available_cols = set(df.columns)
        if "value" not in available_cols:
            raise ValueError(
                "CSV is missing the required 'value' column. Expected columns: "
                "value (required), item_type, public_reference, reference, expire_days."
            )

        # ── Iterate rows, applying defaults, calling Curator (or dry-run) ────
        added: list[dict] = []
        failed: list[dict] = []
        skipped: list[dict] = []

        rows_iter = df.iter_rows(named=True)
        for row_index, row in enumerate(rows_iter, start=2):  # +1 for header, +1 for 1-based
            raw_value = row.get("value")
            value = str(raw_value).strip() if raw_value is not None else ""
            if not value:
                skipped.append({"row": row_index, "reason": "missing value"})
                continue

            raw_item_type = row.get("item_type") if "item_type" in available_cols else None
            item_type = (str(raw_item_type).strip() if raw_item_type is not None else "") or (
                default_item_type or ""
            )
            if item_type not in _VALID_ITEM_TYPES:
                skipped.append({
                    "row": row_index,
                    "value": value,
                    "reason": (
                        f"invalid item_type {item_type!r} "
                        f"(expected one of {list(_VALID_ITEM_TYPES)})"
                    ),
                })
                continue

            raw_pubref = row.get("public_reference") if "public_reference" in available_cols else None
            public_reference = (
                str(raw_pubref).strip() if raw_pubref not in (None, "") else (default_public_reference or None)
            )

            raw_ref = row.get("reference") if "reference" in available_cols else None
            reference = (
                str(raw_ref).strip() if raw_ref not in (None, "") else (default_reference or None)
            )

            expire_days: Optional[int] = None
            raw_expire = row.get("expire_days") if "expire_days" in available_cols else None
            if raw_expire not in (None, ""):
                try:
                    expire_days = int(raw_expire)
                    if expire_days < 1:
                        raise ValueError
                except (TypeError, ValueError):
                    skipped.append({
                        "row": row_index,
                        "value": value,
                        "reason": f"invalid expire_days {raw_expire!r} (must be positive integer)",
                    })
                    continue
            elif default_expire_days is not None:
                expire_days = int(default_expire_days)

            if dry_run:
                added.append({"value": value, "item_type": item_type})
                continue

            ok, err = await self._post_single_item(
                client,
                collection_id=int(collection_id),
                value=value,
                item_type=item_type,
                public_reference=public_reference,
                reference=reference,
                expire_days=expire_days,
            )
            if ok:
                added.append({"value": value, "item_type": item_type})
            else:
                failed.append({
                    "row": row_index,
                    "value": value,
                    "item_type": item_type,
                    "error": err,
                })

        result: dict = {
            "action": "import_csv",
            "collection_id": collection_id,
            "source_file_id": str(file_id),
            "dry_run": dry_run,
            "rows_total": total_rows,
            "added": len(added),
            "failed": len(failed),
            "skipped": len(skipped),
            "csv_meta": {
                "delimiter": meta.get("delimiter"),
                "encoding": meta.get("encoding"),
                "columns": meta.get("columns"),
            },
        }
        # Cap inline echoes so the response stays small even for large imports.
        result["added_sample"] = added[:50]
        if failed:
            result["errors"] = failed[:50]
            result["errors_truncated"] = len(failed) > 50
        if skipped:
            result["skipped_rows"] = skipped[:50]
            result["skipped_truncated"] = len(skipped) > 50
        return result
