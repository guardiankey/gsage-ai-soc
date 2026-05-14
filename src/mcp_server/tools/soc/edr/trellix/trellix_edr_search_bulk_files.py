"""gSage AI — Trellix EDR bulk file hunt tool.

Convenience shortcut over :class:`trellix_edr_search` for hunting MANY
files in a single Trellix search.  Accepts a list of up to
:data:`MAX_BULK_ITEMS` strings — either hashes (MD5/SHA1/SHA256, algorithm
auto-detected per item from the hex length) or file-name substrings — and
issues ONE v1 Active Response search that OR-combines all items.  Much
cheaper than running N separate searches.

Permission: ``edr:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.edr.trellix import _query as Q
from src.mcp_server.tools.soc.edr.trellix._artifacts import build_agent_payload
from src.mcp_server.tools.soc.edr.trellix._client import TrellixEDRError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

MAX_BULK_ITEMS: int = 20


class TrellixEdrSearchBulkFilesTool(BaseTool):
    """Hunt many files in a single search by hash OR file_name.

    Build one v1 Active Response query that ORs every item in ``items``
    together.  Use this instead of calling :class:`trellix_edr_search_files`
    in a loop when you have a list of IoCs (e.g. hashes from a threat
    intel feed) — it cuts N round-trips to one.

    Examples:
        - ``kind="hash"``, ``items=["44d8...02f", "da39...0709", "e3b0...b855"]``
          → matches any of the three (MD5, SHA1, SHA256 auto-detected per
          item).
        - ``kind="file_name"``, ``items=["powershell.exe", "wscript.exe"]``
          → matches files whose full path contains either substring.

    Permission: ``edr:read``
    """

    name: ClassVar[str] = "trellix_edr_search_bulk_files"
    config_namespace: ClassVar[str] = "trellix_edr"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Bulk file hunt across Trellix EDR endpoints by a LIST of hashes "
        "or file-name substrings (single OR-combined search)"
    )
    category: ClassVar[str] = "edr"
    permissions: ClassVar[list[str]] = ["edr:read"]

    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 900
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    always_background: ClassVar[bool] = True

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    config_schema: ClassVar[Optional[dict]] = Q.TRELLIX_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = Q.TRELLIX_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {"target_entities": "items"}
    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["items", "kind"],
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_BULK_ITEMS,
                "items": {"type": "string", "minLength": 1},
                "description": (
                    f"List of up to {MAX_BULK_ITEMS} values to hunt for. "
                    "Each item is matched independently and the results "
                    "are OR-combined into a single Trellix search. "
                    "Duplicates (case-insensitive for hashes) are dropped."
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["hash", "file_name"],
                "description": (
                    "Type of items in the list. 'hash' uses EQUALS on the "
                    "matching hash column (MD5/SHA1/SHA256 auto-detected "
                    "per item by hex length: 32/40/64). 'file_name' uses "
                    "CONTAINS on Files.full_name (substring match on the "
                    "full path)."
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
                    "Persist all rows as a CSV file artifact. PREFER CSV "
                    "over JSON for tabular results. When the caller asks "
                    "to save/export/download without specifying a format, "
                    "set this to true."
                ),
            },
            "export_json": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Persist all rows as JSON. Only when the user "
                    "explicitly asks for JSON — otherwise use 'export_csv'."
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

        raw_items = params.get("items")
        kind = params.get("kind")

        if not isinstance(raw_items, list) or not raw_items:
            return self._failure(
                "INVALID_INPUT",
                "'items' must be a non-empty list of strings.",
            )
        if len(raw_items) > MAX_BULK_ITEMS:
            return self._failure(
                "INVALID_INPUT",
                f"'items' has {len(raw_items)} entries; the maximum is "
                f"{MAX_BULK_ITEMS}. Split the call into smaller batches.",
            )
        if kind not in ("hash", "file_name"):
            return self._failure(
                "INVALID_INPUT",
                "'kind' must be either 'hash' or 'file_name'.",
            )

        # Normalise and deduplicate while preserving order.
        seen: set[str] = set()
        items: list[str] = []
        for raw in raw_items:
            if not isinstance(raw, str):
                return self._failure(
                    "INVALID_INPUT",
                    "Every entry in 'items' must be a string.",
                )
            s = raw.strip()
            if not s:
                continue
            key = s.lower() if kind == "hash" else s
            if key in seen:
                continue
            seen.add(key)
            items.append(s)
        if not items:
            return self._failure(
                "INVALID_INPUT",
                "'items' contains no usable values after trimming.",
            )

        # Build the v1 OR-of-AND condition tree directly.  Each item lands
        # in its own AND-block so Trellix evaluates them as independent
        # alternatives.
        and_blocks: list[dict] = []
        if kind == "hash":
            invalid: list[str] = []
            for value in items:
                detected = Q.detect_hash_type(value)
                if detected is None:
                    invalid.append(value)
                    continue
                hash_type, hash_value = detected
                and_blocks.append({
                    "and": [{
                        "name": "Files",
                        "output": hash_type,
                        "op": "EQUALS",
                        "value": hash_value,
                    }]
                })
            if invalid:
                return self._failure(
                    "INVALID_HASH",
                    "Not a valid MD5/SHA1/SHA256 hex value (expected "
                    f"length 32/40/64): {invalid}",
                )
        else:  # file_name
            for value in items:
                and_blocks.append({
                    "and": [{
                        "name": "Files",
                        "output": "full_name",
                        "op": "CONTAINS",
                        "value": value,
                    }]
                })

        if not and_blocks:
            return self._failure(
                "INVALID_INPUT",
                "No valid items to search for after normalisation.",
            )

        payload = {
            "projections": [
                {"name": "HostInfo", "outputs": ["hostname", "ip_address"]},
                {
                    "name": "Files",
                    "outputs": [
                        "name",
                        "sha1",
                        "sha256",
                        "md5",
                        "status",
                        "full_name",
                        "created_at",
                        "create_user_name",
                    ],
                },
            ],
            "condition": {"or": and_blocks},
        }

        max_rows = Q.clamp_max_rows(params.get("max_rows"))
        export_csv = bool(params.get("export_csv", False))
        export_json = bool(params.get("export_json", False))

        try:
            async with Q.build_client(config) as client:
                query_id, rows, meta, truncated = await Q.run_search_pipeline(
                    client,
                    api_version="v1",
                    payload=payload,
                    max_rows=max_rows,
                )
        except TrellixEDRError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code,
                str(exc),
                retryable=Q.is_retryable_error(exc),
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("trellix_edr_search_bulk_files: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        summary = Q.summarize(
            rows,
            group_by=[
                "HostInfo_hostname",
                "Files_sha1",
                "Files_sha256",
                "Files_md5",
                "Files_status",
            ],
        )
        agent_payload = await build_agent_payload(
            self,
            rows=rows,
            export_csv=export_csv,
            export_json=export_json,
            filename_prefix=f"trellix_edr_bulk_files_{query_id}",
            agent_context=agent_context,
        )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            {
                "query_id": query_id,
                "api_version": "v1",
                "kind": kind,
                "items_searched": len(items),
                "total_count": meta.get("total_count", len(rows)),
                "total_hosts": meta.get("total_hosts", 0),
                "truncated": truncated,
                "artifacts": agent_payload["artifacts"],
                "rows_total": agent_payload["rows_total"],
                "rows_overflow": agent_payload["rows_overflow"],
                "agent_hint": agent_payload["agent_hint"],
                "rows_preview_limit": Q.AGENT_PREVIEW_ROWS,
                "summary": summary,
                "rows": agent_payload["rows_preview"],
            },
            execution_time_ms=elapsed,
        )
