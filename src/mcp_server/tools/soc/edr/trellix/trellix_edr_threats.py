"""gSage AI — Trellix EDR threats tool.

Fetches threats, affected hosts, and detections from the Trellix EDR
threats endpoints.  Threats are aggregated indicators (hashes, filenames)
with severity/rank scoring, distinct from the real-time alert stream.

Endpoints:
    ``GET /edr/v2/threats``                  — list threats (paginated)
    ``GET /edr/v2/threats/{id}``             — single threat detail
    ``GET /edr/v2/threats/{id}/affectedhosts`` — affected hosts
    ``GET /edr/v2/threats/{id}/detections``   — detections

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

# ── Valid actions ───────────────────────────────────────────────────────────

_ACTION_LIST = "list"
_ACTION_DETAIL = "detail"
_ACTION_AFFECTED_HOSTS = "affected_hosts"
_ACTION_DETECTIONS = "detections"
_ACTION_TRACE = "trace"
_VALID_ACTIONS = (_ACTION_LIST, _ACTION_DETAIL, _ACTION_AFFECTED_HOSTS, _ACTION_DETECTIONS, _ACTION_TRACE)

# ── Default group-by keys per action ────────────────────────────────────────

_DEFAULT_THREAT_GROUP_KEYS = (
    "severity", "status", "type", "name",
)
_DEFAULT_AFFECTED_HOST_GROUP_KEYS = (
    "host.hostname", "host.hostOs", "severity",
)
_DEFAULT_DETECTION_GROUP_KEYS = (
    "severity", "host.hostname", "host.hostOs",
)


def _coerce_epoch_ms(value: object) -> Optional[int]:
    """Coerce a trace detection date to epoch milliseconds.

    Accepts:
    - ``int`` — already epoch milliseconds (returned as-is if > 10¹¹).
    - ``str`` — ISO 8601 (e.g. ``"2026-07-10T12:24:35Z"``).
    - ``str`` — numeric epoch milliseconds.
    """
    if isinstance(value, int):
        return value if value > 10**11 else value * 1000  # seconds → ms fallback
    if isinstance(value, float):
        return int(value) if value > 10**11 else int(value * 1000)
    if isinstance(value, str):
        stripped = value.strip()
        # Try numeric first
        try:
            num = int(stripped)
            return num if num > 10**11 else num * 1000
        except ValueError:
            pass
        # Try ISO 8601
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except (ValueError, OSError):
            pass
    return None


class TrellixEdrThreatsTool(BaseTool):
    """Fetch Trellix EDR threats with affected hosts and detections.

    Supports four actions via the ``action`` parameter:

    - ``list`` — paginated list of threats with severity/status/hash filtering.
    - ``detail`` — single threat by numeric ID.
    - ``affected_hosts`` — hosts affected by a specific threat.
    - ``detections`` — individual detections for a specific threat.

    Output (``data``)::

        action, total_resource_count, total_matched, truncated,
        rows_total, rows_overflow, rows_preview_limit, agent_hint,
        summary: { row_count, distinct, top, sample },
        rows: [...up to 100 inlined...],
        artifacts: { csv_file, json_file }
    """

    name: ClassVar[str] = "trellix_edr_threats"
    config_namespace: ClassVar[str] = "trellix_edr"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Fetch Trellix EDR threats (list, detail, affected hosts, detections) "
        "with severity, hash, and host filtering. Supports CSV/JSON export."
    )
    category: ClassVar[str] = "edr"
    permissions: ClassVar[list[str]] = ["edr:read"]

    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 300
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

    audit_field_mapping: ClassVar[dict] = {"target_entities": "name_contains"}
    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_VALID_ACTIONS),
                "default": _ACTION_LIST,
                "description": (
                    "Which threat endpoint to query. "
                    "'list' = paginated threat list, "
                    "'detail' = single threat by ID, "
                    "'affected_hosts' = hosts affected by a threat, "
                    "'detections' = individual detections for a threat, "
                    "'trace' = full process activity timeline for a specific trace "
                    "(requires trace_id, ma_guid, and detection_date_epoch_ms)."
                ),
            },
            "threat_id": {
                "type": "string",
                "description": (
                    "Threat ID (numeric string, e.g. '9257473'). "
                    "Required for action=detail|affected_hosts|detections."
                ),
            },
            "trace_id": {
                "type": "string",
                "description": (
                    "Trace UUID (e.g. from a detection's traceId or alert's Root_Trace_Id). "
                    "Required for action=trace."
                ),
            },
            "ma_guid": {
                "type": "string",
                "description": (
                    "Trellix agent GUID (MAGUID) of the host. "
                    "Required for action=trace."
                ),
            },
            "detection_date_epoch_ms": {
                "type": ["integer", "string"],
                "description": (
                    "Detection date for the trace. Accepts epoch milliseconds (e.g. 1783686275000) "
                    "or ISO 8601 string (e.g. '2026-07-10T12:24:35Z'). "
                    "Get this from a detection's 'firstDetected' field. Required for action=trace."
                ),
            },
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": Q.HARD_MAX_ROWS,
                "default": Q.DEFAULT_MAX_ROWS,
                "description": (
                    f"Maximum number of items to fetch across all pages "
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
                "default": 168,
                "description": (
                    "How many hours back to fetch threats from (only for action='list'). "
                    "Use 0 to omit the time filter."
                ),
            },
            "severity": {
                "type": "string",
                "enum": ["s0", "s1", "s2", "s3", "s4", "s5"],
                "description": "Filter by severity level.",
            },
            "status": {
                "type": "string",
                "description": "Filter by threat status (e.g. 'viewed', 'open', 'resolved').",
            },
            "name_contains": {
                "type": "string",
                "description": "Filter threats by name substring (case-insensitive).",
            },
            "hash": {
                "type": "string",
                "description": (
                    "Filter by hash (MD5 32-char, SHA1 40-char, or SHA256 64-char hex). "
                    "Auto-detected by length."
                ),
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
            "include_trace": {
                "type": "boolean",
                "default": False,
                "description": (
                    "For action='detections': enrich each detection (up to 20) with the full "
                    "process activity timeline. Adds 'trace_items' and 'trace_items_count' fields. "
                    "Uses traceId + host.aGuid + firstDetected from each detection."
                ),
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

        # ── 1. Extract & validate params ───────────────────────────────
        action = params.get("action", _ACTION_LIST)
        if action not in _VALID_ACTIONS:
            return self._failure(
                "INVALID_INPUT",
                f"Invalid action '{action}'. Must be one of: {', '.join(_VALID_ACTIONS)}.",
            )

        threat_id = params.get("threat_id")
        if action in (_ACTION_DETAIL, _ACTION_AFFECTED_HOSTS, _ACTION_DETECTIONS) and not threat_id:
            return self._failure(
                "INVALID_INPUT",
                f"'threat_id' is required for action='{action}'.",
            )

        # Trace-specific params (auto-detect epoch ms vs ISO 8601)
        trace_id_param = params.get("trace_id")
        ma_guid = params.get("ma_guid")
        detection_date_raw = params.get("detection_date_epoch_ms")
        detection_date_epoch_ms: Optional[int] = None
        if action == _ACTION_TRACE:
            if detection_date_raw is not None:
                detection_date_epoch_ms = _coerce_epoch_ms(detection_date_raw)
            if not trace_id_param or not ma_guid or detection_date_epoch_ms is None:
                return self._failure(
                    "INVALID_INPUT",
                    "'trace_id', 'ma_guid', and 'detection_date_epoch_ms' (epoch ms or ISO 8601) "
                    "are required for action='trace'.",
                )

        max_rows = Q.clamp_max_rows(params.get("max_rows"))
        page_limit = max(1, min(int(params.get("page_limit", 100) or 100), 500))
        lookback_h = int(params.get("lookback_hours", 168) or 168)
        severity = params.get("severity")
        status_filter = params.get("status")
        name_contains = params.get("name_contains")
        hash_value = params.get("hash")
        sort = params.get("sort", "-rank")
        export_csv = bool(params.get("export_csv", False))
        export_json = bool(params.get("export_json", False))
        group_by = params.get("group_by") or None
        top_n = int(params.get("top_n", 10) or 10)

        # Detect hash type for client-side filtering
        hash_type: Optional[str] = None
        hash_normalised: Optional[str] = None
        if hash_value:
            detected = Q.detect_hash_type(hash_value)
            if detected:
                hash_type, hash_normalised = detected

        # ── 2. Dispatch by action ──────────────────────────────────────
        try:
            async with Q.build_client(config) as client:
                if action == _ACTION_LIST:
                    rows, total_rc, filename_prefix = await self._list_threats(
                        client, max_rows, page_limit, lookback_h, sort,
                    )
                elif action == _ACTION_DETAIL:
                    rows, total_rc, filename_prefix = await self._get_detail(
                        client, threat_id,  # type: ignore[arg-type]
                    )
                elif action == _ACTION_AFFECTED_HOSTS:
                    rows, total_rc, filename_prefix = await self._get_affected_hosts(
                        client, threat_id, max_rows, page_limit,  # type: ignore[arg-type]
                    )
                elif action == _ACTION_DETECTIONS:
                    rows, total_rc, filename_prefix = await self._get_detections(
                        client, threat_id, max_rows, page_limit,  # type: ignore[arg-type]
                    )
                else:  # _ACTION_TRACE
                    assert detection_date_epoch_ms is not None  # validated above
                    rows, total_rc, filename_prefix = await self._get_trace(
                        client,
                        trace_id=trace_id_param,  # type: ignore[arg-type]
                        ma_guid=ma_guid,  # type: ignore[arg-type]
                        detection_date_epoch_ms=detection_date_epoch_ms,
                    )
        except TrellixEDRError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code, str(exc),
                retryable=Q.is_retryable_error(exc),
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("trellix_edr_threats: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        # ── 3. Client-side filtering ───────────────────────────────────
        rows = self._apply_filters(
            rows,
            severity=severity,
            status_filter=status_filter,
            name_contains=name_contains,
            hash_type=hash_type,
            hash_normalised=hash_normalised,
        )
        truncated = len(rows) >= max_rows
        total_after_filter = len(rows)

        # ── 4. Summarise ───────────────────────────────────────────────
        if group_by is None:
            if action == _ACTION_LIST:
                group_by = list(_DEFAULT_THREAT_GROUP_KEYS)
            elif action == _ACTION_AFFECTED_HOSTS:
                group_by = list(_DEFAULT_AFFECTED_HOST_GROUP_KEYS)
            elif action == _ACTION_DETECTIONS:
                group_by = list(_DEFAULT_DETECTION_GROUP_KEYS)
            else:
                group_by = list(_DEFAULT_THREAT_GROUP_KEYS)
        summary = Q.summarize(rows, group_by=group_by, top_n=top_n)

        # ── 5. Build agent payload ─────────────────────────────────────
        agent_payload = await build_agent_payload(
            self,
            rows=rows,
            export_csv=export_csv,
            export_json=export_json,
            filename_prefix=filename_prefix,
            agent_context=agent_context,
        )

        # ── 6. Success response ────────────────────────────────────────
        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            {
                "action": action,
                "total_resource_count": total_rc,
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

    # ── Action: list ────────────────────────────────────────────────────────

    async def _list_threats(
        self,
        client: TrellixEDRClient,
        max_rows: int,
        page_limit: int,
        lookback_hours: int,
        sort: str,
    ) -> tuple[list[dict], int, str]:
        """Paginate GET /edr/v2/threats."""
        from_ms: Optional[int] = None
        to_ms: Optional[int] = None
        if lookback_hours > 0:
            now_ms = int(time.time() * 1000)
            from_ms = now_ms - (lookback_hours * 3600 * 1000)
            to_ms = now_ms

        all_rows: list[dict] = []
        offset = 0
        total_rc = 0

        while len(all_rows) < max_rows:
            body = await client.get_threats(
                page_offset=offset,
                page_limit=min(page_limit, max_rows - len(all_rows)),
                from_ms=from_ms,
                to_ms=to_ms,
                sort=sort,
            )
            data = body.get("data", [])
            if not isinstance(data, list) or len(data) == 0:
                break

            for item in data:
                all_rows.append(self._flatten_threat(item))

            if offset == 0:
                total_rc = int(body.get("meta", {}).get("totalResourceCount", 0))

            links = body.get("links", {})
            next_url = links.get("next") if isinstance(links, dict) else None
            if not next_url:
                break
            offset += len(data)

        return all_rows, total_rc, "trellix_edr_threats_list"

    # ── Action: detail ──────────────────────────────────────────────────────

    async def _get_detail(
        self,
        client: TrellixEDRClient,
        threat_id: str,
    ) -> tuple[list[dict], int, str]:
        """GET /edr/v2/threats/{id} — single threat."""
        body = await client.get_threat_by_id(threat_id)
        data = body.get("data", {})
        if isinstance(data, dict) and data:
            flat = self._flatten_threat(data)
            return [flat], 1, f"trellix_edr_threats_{threat_id}"
        return [], 0, f"trellix_edr_threats_{threat_id}"

    # ── Action: affected_hosts ──────────────────────────────────────────────

    async def _get_affected_hosts(
        self,
        client: TrellixEDRClient,
        threat_id: str,
        max_rows: int,
        page_limit: int,
    ) -> tuple[list[dict], int, str]:
        """Paginate GET /edr/v2/threats/{id}/affectedhosts."""
        all_rows: list[dict] = []
        offset = 0
        total_rc = 0

        while len(all_rows) < max_rows:
            body = await client.get_affected_hosts(
                threat_id,
                page_offset=offset,
                page_limit=min(page_limit, max_rows - len(all_rows)),
            )
            data = body.get("data", [])
            if not isinstance(data, list) or len(data) == 0:
                break

            for item in data:
                all_rows.append(self._flatten_affected_host(item))

            if offset == 0:
                total_rc = int(body.get("meta", {}).get("totalResourceCount", 0))

            links = body.get("links", {})
            next_url = links.get("next") if isinstance(links, dict) else None
            if not next_url:
                break
            offset += len(data)

        return all_rows, total_rc, f"trellix_edr_threats_{threat_id}_hosts"

    # ── Action: detections ──────────────────────────────────────────────────

    async def _get_detections(
        self,
        client: TrellixEDRClient,
        threat_id: str,
        max_rows: int,
        page_limit: int,
    ) -> tuple[list[dict], int, str]:
        """Paginate GET /edr/v2/threats/{id}/detections."""
        all_rows: list[dict] = []
        offset = 0
        total_rc = 0

        while len(all_rows) < max_rows:
            body = await client.get_detections_by_threat(
                threat_id,
                page_offset=offset,
                page_limit=min(page_limit, max_rows - len(all_rows)),
            )
            data = body.get("data", [])
            if not isinstance(data, list) or len(data) == 0:
                break

            for item in data:
                all_rows.append(self._flatten_detection(item))

            if offset == 0:
                total_rc = int(body.get("meta", {}).get("totalResourceCount", 0))

            links = body.get("links", {})
            next_url = links.get("next") if isinstance(links, dict) else None
            if not next_url:
                break
            offset += len(data)

        return all_rows, total_rc, f"trellix_edr_threats_{threat_id}_detections"

    # ── Action: trace ───────────────────────────────────────────────────────

    async def _get_trace(
        self,
        client: TrellixEDRClient,
        *,
        trace_id: str,
        ma_guid: str,
        detection_date_epoch_ms: int,
    ) -> tuple[list[dict], int, str]:
        """Fetch the full process activity timeline for a trace."""
        body = await client.get_trace_activity(
            trace_id=trace_id,
            ma_guid=ma_guid,
            detection_date_epoch_ms=detection_date_epoch_ms,
        )
        items = (
            body.get("data", {})
            .get("attributes", {})
            .get("items", [])
        )
        if not isinstance(items, list):
            return [], 0, f"trellix_edr_trace_{trace_id[:8]}"

        rows: list[dict] = []
        for item in items:
            flat: dict[str, object] = {
                "eventType": item.get("eventType", "?"),
                "processName": item.get("processName", ""),
                "host": item.get("host", ""),
            }
            cmd = item.get("cmdLine", "")
            if cmd:
                flat["cmdLine"] = cmd
            sev = item.get("severity", "")
            if sev:
                flat["severity"] = sev
            tags = item.get("tags", [])
            if tags:
                flat["tags"] = ", ".join(str(t) for t in tags)
            pid = item.get("pid")
            if pid is not None:
                flat["pid"] = pid
            ppid = item.get("ppid")
            if ppid is not None:
                flat["ppid"] = ppid
            user = item.get("user", {})
            if isinstance(user, dict) and user.get("name"):
                flat["user"] = user["name"]
            dsets = item.get("detectionsSets", [])
            if dsets:
                flat["detectionsSets"] = ", ".join(
                    f"{ds.get('sev','?')}:{','.join(ds.get('tags',[])[:3])}"
                    for ds in dsets[:3] if isinstance(ds, dict)
                )
            pfa = item.get("procFileAttrs", {})
            if isinstance(pfa, dict) and pfa.get("sha256"):
                flat["sha256"] = pfa["sha256"]
            rows.append(flat)

        return rows, len(rows), f"trellix_edr_trace_{trace_id[:8]}"

    # ── Flatten helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _flatten_threat(threat: dict) -> dict[str, Any]:
        """Flatten a JSON:API threat into a single-level dict."""
        attrs = threat.get("attributes", {}) if isinstance(threat.get("attributes"), dict) else {}
        flat: dict[str, Any] = {"id": threat.get("id", "")}

        for key, value in attrs.items():
            if isinstance(value, dict):
                # hashes.{sha256, sha1, md5}
                for sub_key, sub_value in value.items():
                    flat[f"{key}.{sub_key}"] = sub_value
            elif isinstance(value, list):
                flat[key] = ", ".join(str(v) for v in value)
            else:
                flat[key] = value

        return flat

    @staticmethod
    def _flatten_affected_host(host: dict) -> dict[str, Any]:
        """Flatten a JSON:API affected-host into a single-level dict."""
        attrs = host.get("attributes", {}) if isinstance(host.get("attributes"), dict) else {}
        flat: dict[str, Any] = {"id": host.get("id", "")}

        for key, value in attrs.items():
            if key == "host" and isinstance(value, dict):
                # host.{hostname, hostOs, aGuid, os.{...}, netInterfaces[...], lastBootTime}
                for hk, hv in value.items():
                    if isinstance(hv, dict):
                        for sub_k, sub_v in hv.items():
                            flat[f"host.{hk}.{sub_k}"] = sub_v
                    elif isinstance(hv, list):
                        # netInterfaces — join as comma-separated IPs
                        if hk == "netInterfaces":
                            ips = [ni.get("ip", "?") for ni in hv if isinstance(ni, dict)]
                            flat["host.netInterfaces.ips"] = ", ".join(ips)
                        else:
                            flat[f"host.{hk}"] = ", ".join(str(x) for x in hv)
                    else:
                        flat[f"host.{hk}"] = hv
            elif isinstance(value, list):
                flat[key] = ", ".join(str(v) for v in value)
            else:
                flat[key] = value

        return flat

    @staticmethod
    def _flatten_detection(detection: dict) -> dict[str, Any]:
        """Flatten a JSON:API detection into a single-level dict."""
        attrs = detection.get("attributes", {}) if isinstance(detection.get("attributes"), dict) else {}
        flat: dict[str, Any] = {"id": detection.get("id", "")}

        for key, value in attrs.items():
            if key == "host" and isinstance(value, dict):
                for hk, hv in value.items():
                    if isinstance(hv, dict):
                        for sub_k, sub_v in hv.items():
                            flat[f"host.{hk}.{sub_k}"] = sub_v
                    elif isinstance(hv, list):
                        flat[f"host.{hk}"] = ", ".join(str(x) for x in hv)
                    else:
                        flat[f"host.{hk}"] = hv
            elif isinstance(value, list):
                flat[key] = ", ".join(str(v) for v in value)
            elif isinstance(value, dict):
                for sub_k, sub_v in value.items():
                    flat[f"{key}.{sub_k}"] = sub_v
            else:
                flat[key] = value

        return flat

    # ── Client-side filters ──────────────────────────────────────────────────

    @staticmethod
    def _apply_filters(
        rows: list[dict],
        *,
        severity: Optional[str] = None,
        status_filter: Optional[str] = None,
        name_contains: Optional[str] = None,
        hash_type: Optional[str] = None,
        hash_normalised: Optional[str] = None,
    ) -> list[dict]:
        """Apply client-side filters to threat/host/detection rows."""
        filtered = rows
        if severity:
            filtered = [r for r in filtered if str(r.get("severity", "")).lower() == severity.lower()]
        if status_filter:
            filtered = [r for r in filtered if str(r.get("status", "")).lower() == status_filter.lower()]
        if name_contains:
            q = name_contains.lower()
            filtered = [r for r in filtered if q in str(r.get("name", "")).lower()]
        if hash_type and hash_normalised:
            # Match against hashes.{md5,sha1,sha256}
            hash_field = f"hashes.{hash_type}"
            filtered = [r for r in filtered if str(r.get(hash_field, "")).lower() == hash_normalised]
        return filtered
