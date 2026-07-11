"""gSage AI — Trellix EDR alerts tool (v3).

Fetches and summarises alerts from ``/edr/v3/alerts``.  Alerts are a
poll-free, direct GET endpoint — no search-queue/303 polling needed.
Results are paginated via JSON:API ``links.next``.

Permission: ``edr:read``
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.edr.trellix import _query as Q
from src.mcp_server.tools.soc.edr.trellix._artifacts import build_agent_payload
from src.mcp_server.tools.soc.edr.trellix._client import TrellixEDRClient, TrellixEDRError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

# ── Default group-by keys for alert summarisation ───────────────────────────

_DEFAULT_ALERT_GROUP_KEYS = (
    "Severity",
    "Activity",
    "Host_Name",
    "Host_OS",
    "ProcessName",
    "User.domain",
    "User.name",
    "RuleId",
)


class TrellixEdrAlertsTool(BaseTool):
    """Fetch Trellix EDR alerts (v3) with filtering, summarisation, and CSV/JSON export.

    Alerts are retrieved from ``GET /edr/v3/alerts`` (enriched with HostInfo).
    Supports client-side filtering by severity, hostname, process name,
    activity, and root trace ID.  Results are paginated, flattened,
    summarised, and optionally exported as CSV/JSON artifacts.

    Output (``data``)::

        api_version, total_resource_count, total_matched, truncated,
        rows_total, rows_overflow, rows_preview_limit, agent_hint,
        summary: { row_count, distinct, top, sample },
        rows: [...up to 100 inlined...],
        artifacts: { csv_file, json_file }
    """

    name: ClassVar[str] = "trellix_edr_alerts"
    config_namespace: ClassVar[str] = "trellix_edr"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Fetch Trellix EDR alerts (v3) with severity, host, process, "
        "and MITRE tag filtering. Supports CSV/JSON export."
    )
    category: ClassVar[str] = "edr"
    permissions: ClassVar[list[str]] = ["edr:read"]

    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 120
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    always_background: ClassVar[bool] = False  # direct GET — fast, no polling

    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True
    config_schema: ClassVar[Optional[dict]] = Q.TRELLIX_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = Q.TRELLIX_CONFIG_DEFAULTS

    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    audit_field_mapping: ClassVar[dict] = {"target_entities": "hostname_contains"}
    audit_output: ClassVar[bool] = False  # too verbose for audit

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": Q.HARD_MAX_ROWS,
                "default": Q.DEFAULT_MAX_ROWS,
                "description": (
                    f"Maximum number of alerts to fetch across all pages "
                    f"(default {Q.DEFAULT_MAX_ROWS}, hard cap {Q.HARD_MAX_ROWS})."
                ),
            },
            "page_limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "default": 100,
                "description": "Page size for each API request.",
            },
            "lookback_hours": {
                "type": "integer",
                "minimum": 0,
                "maximum": 168,
                "default": 24,
                "description": (
                    "How many hours back to fetch alerts from. "
                    "Use 0 to omit the time filter (server default window)."
                ),
            },
            "severity": {
                "type": "string",
                "description": "Filter by severity (e.g. 's0', 's1', 's2', 's3', 's4', 's5').",
            },
            "hostname_contains": {
                "type": "string",
                "description": "Filter alerts where Host_Name contains this substring (case-insensitive).",
            },
            "hostname_equals": {
                "type": "string",
                "description": "Filter alerts where Host_Name equals this value exactly (case-insensitive).",
            },
            "process_name_contains": {
                "type": "string",
                "description": "Filter alerts where ProcessName contains this substring.",
            },
            "activity": {
                "type": "string",
                "description": "Filter by Activity field (e.g. 'Threat Detected').",
            },
            "root_trace_id": {
                "type": "string",
                "description": "Filter by Root_Trace_Id (exact match).",
            },
            "sort": {
                "type": "string",
                "enum": ["rank", "-rank"],
                "default": "-rank",
                "description": "Sort order: 'rank' (ascending) or '-rank' (descending, default).",
            },
            "export_csv": {
                "type": "boolean",
                "default": False,
                "description": "Export full results as CSV artifact.",
            },
            "export_json": {
                "type": "boolean",
                "default": False,
                "description": "Export full results as JSON artifact.",
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Columns to compute distinct counts and top-N for in the summary.",
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
                "description": "Number of top values to return per group_by column.",
            },
        },
        "additionalProperties": False,
    }

    # ── execute ─────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()

        # ── 1. Extract params ──────────────────────────────────────────
        max_rows = Q.clamp_max_rows(params.get("max_rows"))
        page_limit = max(1, min(int(params.get("page_limit", 100) or 100), 500))
        lookback_h = int(params.get("lookback_hours", 24) or 24)
        severity = params.get("severity")
        host_contains = params.get("hostname_contains")
        host_equals = params.get("hostname_equals")
        proc_contains = params.get("process_name_contains")
        activity = params.get("activity")
        root_trace = params.get("root_trace_id")
        sort = params.get("sort", "-rank")
        export_csv = bool(params.get("export_csv", False))
        export_json = bool(params.get("export_json", False))
        group_by = params.get("group_by") or list(_DEFAULT_ALERT_GROUP_KEYS)
        top_n = int(params.get("top_n", 10) or 10)

        # ── 2. Compute time range (milliseconds) ───────────────────────
        from_ms: Optional[int] = None
        to_ms: Optional[int] = None
        if lookback_h > 0:
            now_ms = int(time.time() * 1000)
            from_ms = now_ms - (lookback_h * 3600 * 1000)
            to_ms = now_ms

        # ── 3. Fetch alerts (paginated) ────────────────────────────────
        try:
            async with Q.build_client(config) as client:
                rows, total_resource_count = await self._fetch_all_pages(
                    client,
                    page_limit=page_limit,
                    max_rows=max_rows,
                    from_ms=from_ms,
                    to_ms=to_ms,
                    sort=sort,
                    filter_str=None,
                )
        except TrellixEDRError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code, str(exc),
                retryable=Q.is_retryable_error(exc),
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("trellix_edr_alerts: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        # ── 4. Client-side filtering ───────────────────────────────────
        rows = self._apply_filters(
            rows,
            severity=severity,
            host_contains=host_contains,
            host_equals=host_equals,
            proc_contains=proc_contains,
            activity=activity,
            root_trace=root_trace,
        )
        truncated = len(rows) >= max_rows
        total_after_filter = len(rows)

        # ── 5. Summarise ───────────────────────────────────────────────
        summary = Q.summarize(rows, group_by=group_by, top_n=top_n)

        # ── 6. Build agent payload (preview + CSV/JSON artifacts) ──────
        agent_payload = await build_agent_payload(
            self,
            rows=rows,
            export_csv=export_csv,
            export_json=export_json,
            filename_prefix="trellix_edr_alerts",
            agent_context=agent_context,
        )

        # ── 7. Success response ────────────────────────────────────────
        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            {
                "api_version": "v3",
                "total_resource_count": total_resource_count,
                "total_matched": total_after_filter,
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

    # ── Pagination helper ────────────────────────────────────────────────────

    async def _fetch_all_pages(
        self,
        client: TrellixEDRClient,
        *,
        page_limit: int,
        max_rows: int,
        from_ms: Optional[int],
        to_ms: Optional[int],
        sort: Optional[str],
        filter_str: Optional[str],
    ) -> tuple[list[dict], int]:
        """Fetch alerts across all pages up to max_rows.

        Returns ``(flat_rows, total_resource_count)``.
        """
        all_rows: list[dict] = []
        offset = 0
        total_resource_count = 0

        while len(all_rows) < max_rows:
            body = await client.get_alerts(
                page_offset=offset,
                page_limit=min(page_limit, max_rows - len(all_rows)),
                from_ms=from_ms,
                to_ms=to_ms,
                sort=sort,
                filter_str=filter_str,
            )
            data = body.get("data", [])
            if not isinstance(data, list) or len(data) == 0:
                break

            for alert in data:
                all_rows.append(self._flatten_alert(alert))

            # Capture totalResourceCount from first page
            if offset == 0:
                meta = body.get("meta", {})
                total_resource_count = int(meta.get("totalResourceCount", 0))

            # Paginate via links.next (totalResourceCount is deprecated)
            links = body.get("links", {})
            next_url = links.get("next") if isinstance(links, dict) else None
            if not next_url:
                break
            offset += len(data)

        return all_rows, total_resource_count

    # ── Flatten ──────────────────────────────────────────────────────────────

    @staticmethod
    def _flatten_alert(alert: dict) -> dict[str, Any]:
        """Flatten a JSON:API alert into a single-level dict.

        ``{"type": "alerts", "id": "...", "attributes": {Severity, Host_Name, ...}}``
        →
        ``{"id": "...", "Severity": "s0", "Host_Name": "PC1", ..., "User.domain": "DOMAIN", "User.name": "user"}``
        """
        attrs = alert.get("attributes", {}) if isinstance(alert.get("attributes"), dict) else {}
        flat: dict[str, Any] = {"id": alert.get("id", "")}

        for key, value in attrs.items():
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    flat[f"{key}.{sub_key}"] = sub_value
            elif isinstance(value, list):
                flat[key] = ", ".join(str(v) for v in value)
            else:
                flat[key] = value

        return flat

    # ── Client-side filters ──────────────────────────────────────────────────

    @staticmethod
    def _apply_filters(
        rows: list[dict],
        *,
        severity: Optional[str] = None,
        host_contains: Optional[str] = None,
        host_equals: Optional[str] = None,
        proc_contains: Optional[str] = None,
        activity: Optional[str] = None,
        root_trace: Optional[str] = None,
    ) -> list[dict]:
        """Apply client-side filters (case-insensitive where applicable).

        Server-side ``filter`` query param has undocumented syntax;
        client-side filtering on flattened rows gives reliable behaviour.
        """
        filtered = rows
        if severity:
            filtered = [r for r in filtered if str(r.get("Severity", "")).lower() == severity.lower()]
        if host_contains:
            q = host_contains.lower()
            filtered = [r for r in filtered if q in str(r.get("Host_Name", "")).lower()]
        if host_equals:
            q = host_equals.lower()
            filtered = [r for r in filtered if str(r.get("Host_Name", "")).lower() == q]
        if proc_contains:
            q = proc_contains.lower()
            filtered = [r for r in filtered if q in str(r.get("ProcessName", "")).lower()]
        if activity:
            q = activity.lower()
            filtered = [r for r in filtered if str(r.get("Activity", "")).lower() == q]
        if root_trace:
            filtered = [r for r in filtered if str(r.get("Root_Trace_Id", "")) == root_trace]
        return filtered
