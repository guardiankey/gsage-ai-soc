"""gSage AI — Trellix EDR bulk network flow hunt.

Convenience shortcut over :class:`trellix_edr_search_network` for hunting
MANY network indicators in a single Trellix v1 search.  Accepts a list of
up to :data:`MAX_BULK_ITEMS` destination IPs **or** process-name
substrings and OR-combines them into one Active Response query.

No shared AND filters are exposed: Trellix v1 conditions behave poorly
when OR and AND are mixed, so this tool emits a pure OR-of-leaves payload.
Use :class:`trellix_edr_search_network` when you need extra filters.

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


class TrellixEdrSearchBulkNetworkTool(BaseTool):
    """Hunt many network flows in one search by remote IP OR process name.

    Build one v1 Active Response query that ORs every item in ``items``
    together against the NetworkFlow collector.  Use this instead of
    calling :class:`trellix_edr_search_network` in a loop when you have
    a list of IoCs (e.g. C2 IPs from a threat intel feed) — it cuts N
    round-trips to one.

    Examples:
        - ``kind="remote_ip"``,
          ``items=["203.0.113.5", "198.51.100.7"]`` →
          every host that talked to either IP on either side of the flow.
        - ``kind="process_name"``,
          ``items=["powershell", "wscript", "mshta"]`` →
          flows whose process image matches any of those substrings.

    Permission: ``edr:read``
    """

    name: ClassVar[str] = "trellix_edr_search_bulk_network"
    config_namespace: ClassVar[str] = "trellix_edr"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Bulk network flow hunt across Trellix EDR endpoints by a LIST of "
        "remote IPs or process-name substrings (single OR-combined search)"
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
                    "Duplicates are dropped (case-insensitive)."
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["remote_ip", "process_name"],
                "description": (
                    "Type of items in the list. 'remote_ip' uses EQUALS on "
                    "either NetworkFlow.src_ip OR NetworkFlow.dst_ip "
                    "(direction-agnostic). 'process_name' uses CONTAINS on "
                    "NetworkFlow.process (substring match on the image name)."
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
        if kind not in ("remote_ip", "process_name"):
            return self._failure(
                "INVALID_INPUT",
                "'kind' must be either 'remote_ip' or 'process_name'.",
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
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(s)
        if not items:
            return self._failure(
                "INVALID_INPUT",
                "'items' contains no usable values after trimming.",
            )

        # Pure OR-of-leaves: each leaf becomes one AND-block with a single
        # condition inside the outer OR. Trellix v1 misbehaves when OR and
        # AND are mixed, so we deliberately avoid shared AND filters here.
        leaves: list[dict] = []
        if kind == "remote_ip":
            for value in items:
                leaves.append({
                    "name": "NetworkFlow",
                    "output": "src_ip",
                    "op": "EQUALS",
                    "value": value,
                })
                leaves.append({
                    "name": "NetworkFlow",
                    "output": "dst_ip",
                    "op": "EQUALS",
                    "value": value,
                })
        else:  # process_name
            for value in items:
                leaves.append({
                    "name": "NetworkFlow",
                    "output": "process",
                    "op": "CONTAINS",
                    "value": value,
                })

        or_blocks = [{"and": [leaf]} for leaf in leaves]
        if not or_blocks:
            return self._failure(
                "INVALID_INPUT",
                "No valid items to search for after normalisation.",
            )

        payload = {
            "projections": [
                {"name": "HostInfo", "outputs": ["hostname", "ip_address"]},
                {
                    "name": "NetworkFlow",
                    "outputs": [
                        "src_ip",
                        "src_port",
                        "dst_ip",
                        "dst_port",
                        "proto",
                        "direction",
                        "status",
                        "time",
                        "process",
                        "process_id",
                        "user",
                        "sha256",
                    ],
                },
            ],
            "condition": {"or": or_blocks},
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
            log.exception("trellix_edr_search_bulk_network: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        summary = Q.summarize(
            rows,
            group_by=[
                "HostInfo_hostname",
                "NetworkFlow_src_ip",
                "NetworkFlow_dst_ip",
                "NetworkFlow_dst_port",
                "NetworkFlow_process",
            ],
        )
        agent_payload = await build_agent_payload(
            self,
            rows=rows,
            export_csv=export_csv,
            export_json=export_json,
            filename_prefix=f"trellix_edr_bulk_network_{query_id}",
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
