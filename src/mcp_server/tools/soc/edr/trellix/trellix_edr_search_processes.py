"""gSage AI — Trellix EDR process hunt tool.

Hunt running and/or terminated processes across the fleet using the Trellix
``Processes`` (running) and ``ProcessHistory`` (terminated/historical)
collectors.  The tool exposes hunting-friendly filters (process name,
hash, cmdline, parent, user, suspicious reputation, execution mode,
date range) and is optimized for two complementary modes:

- ``include_host_info=False`` (default, fleet-wide hunting):
    The projection contains only the process collector, so Trellix returns
    one aggregated row per distinct process tuple (e.g. one row per unique
    SHA1 currently running in the org).  Compact and ideal for hunting at
    scale ("show me every SHA1 of running powershell.exe in the fleet").

- ``include_host_info=True`` (host-aware investigation):
    Adds the ``HostInfo`` projection.  Rows are duplicated per host, which
    answers "on which hosts is this process running".  Use ONLY with a
    narrow filter (specific hash, exact hostname, etc.) — broad queries in
    this mode can exceed the Trellix API limits and truncate results.

Permission: ``edr:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.edr.trellix import _query as Q
from src.mcp_server.tools.soc.edr.trellix._artifacts import maybe_export
from src.mcp_server.tools.soc.edr.trellix._client import TrellixEDRError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


def _normalize_rows(rows: list[dict], source: str) -> list[dict]:
    """Tag rows with their source collector and unify ProcessHistory_*
    field names to Processes_* for consistent downstream handling.
    """
    out: list[dict] = []
    for r in rows:
        new = {"source": source}
        for k, v in r.items():
            if k.startswith("ProcessHistory_"):
                new["Processes_" + k[len("ProcessHistory_"):]] = v
            else:
                new[k] = v
        out.append(new)
    return out


class TrellixEdrSearchProcessesTool(BaseTool):
    """Hunt processes across the fleet with the Processes/ProcessHistory collectors.

    Common patterns:

    - List every SHA1 of running powershell instances in the org (aggregated)::

        process_name="powershell"      # CONTAINS
        include_host_info=False        # default

    - Find which hosts run a known-bad hash::

        hash="<sha256>"
        include_host_info=True

    - Hunt suspicious reputations across both running and terminated processes::

        suspicious_reputation_only=True
        scope="both"

    - PowerShell IoA hunt by parent_cmdline (running only)::

        parent_cmdline_contains="-nop -w hidden -enc"
        scope="running"

    Permission: ``edr:read``
    """

    name: ClassVar[str] = "trellix_edr_search_processes"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Hunt processes across Trellix EDR endpoints (Processes / ProcessHistory). "
        "Default returns aggregated rows for fleet-wide hunting; set "
        "include_host_info=True to break down by host (use with narrow filters)."
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

    audit_field_mapping: ClassVar[dict] = {
        "target_entities": "hostname_equals",
    }
    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["running", "history", "both"],
                "default": "running",
                "description": (
                    "Which collector to query. 'running' uses Processes (live "
                    "processes only). 'history' uses ProcessHistory (running + "
                    "terminated, retained until reboot, capped at 2000 events). "
                    "'both' executes two queries and merges results, tagging "
                    "each row with 'source'."
                ),
            },
            "process_name": {
                "type": "string",
                "description": "Substring match on the process name (Processes.name CONTAINS).",
            },
            "process_name_equals": {
                "type": "string",
                "description": "Exact match on the process name (Processes.name EQUALS).",
            },
            "cmdline_contains": {
                "type": "string",
                "description": "Substring on the process command line (Processes.cmdline CONTAINS).",
            },
            "parent_cmdline_contains": {
                "type": "string",
                "description": (
                    "Substring on the parent's command line (Processes.parent_cmdline "
                    "CONTAINS). Available only on the Processes collector — ignored "
                    "for 'history' scope."
                ),
            },
            "parent_name": {
                "type": "string",
                "description": "Exact match on the parent process name (Processes.parentname EQUALS).",
            },
            "parent_name_not_equals": {
                "type": "string",
                "description": (
                    "Negative match on parent process name "
                    "(Processes.parentname NOT_EQUALS). Useful to spot anomalous "
                    "parent → child chains, e.g. svchost.exe whose parent is "
                    "not services.exe."
                ),
            },
            "user": {
                "type": "string",
                "description": "Exact match on the user that started the process (Processes.user EQUALS).",
            },
            "hash": {
                "type": "string",
                "description": (
                    "Process hash (32 hex chars=MD5, 40=SHA1, 64=SHA256). "
                    "Algorithm is auto-detected from the input length."
                ),
            },
            "imagepath_contains": {
                "type": "string",
                "description": "Substring on the process image path (Processes.imagepath CONTAINS).",
            },
            "execution_mode": {
                "type": "string",
                "enum": list(Q.PROCESS_EXECUTION_MODES),
                "description": (
                    "Filter by PowerShell execution mode (Processes.execution_mode EQUALS). "
                    "'Commandline' / 'File' are common fileless-vs-file indicators."
                ),
            },
            "suspicious_reputation_only": {
                "type": "boolean",
                "default": False,
                "description": (
                    "If true, restrict to processes whose process_reputation is "
                    "'Known Malicious', 'Most Likely Malicious' or 'Might Be Malicious'."
                ),
            },
            "started_after": {
                "type": "string",
                "description": (
                    "Filter Processes.started_at GREATER_EQUAL <value>. "
                    "ISO-8601 timestamp (e.g. '2026-04-15T00:00:00Z')."
                ),
            },
            "started_before": {
                "type": "string",
                "description": (
                    "Filter Processes.started_at LESS_EQUAL <value>. ISO-8601 timestamp."
                ),
            },
            "hostname_contains": {
                "type": "string",
                "description": "Substring match on hostname (HostInfo.hostname CONTAINS).",
            },
            "hostname_equals": {
                "type": "string",
                "description": "Exact match on hostname (HostInfo.hostname EQUALS).",
            },
            "include_host_info": {
                "type": "boolean",
                "default": False,
                "description": (
                    "When false (default), the projection omits HostInfo and "
                    "Trellix returns aggregated rows per process tuple — best "
                    "for fleet-wide hunting (one row per unique SHA1/cmdline). "
                    "When true, HostInfo is included and rows are duplicated "
                    "per host; ONLY use with a narrow filter (specific hash, "
                    "exact hostname, etc.) to avoid exceeding API limits."
                ),
            },
            "include_powershell_content": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Include PowerShell script content fields (content, "
                    "content_size, content_file) in the projection. Disabled by "
                    "default because content can be up to 8 KB per row."
                ),
            },
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": Q.HARD_MAX_ROWS,
                "default": Q.DEFAULT_MAX_ROWS,
            },
            "export_csv": {"type": "boolean", "default": False},
            "export_json": {"type": "boolean", "default": False},
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

        scope = params.get("scope") or "running"
        if scope not in ("running", "history", "both"):
            return self._failure("INVALID_INPUT", f"Unknown scope '{scope}'.")

        process_name = (params.get("process_name") or "").strip() or None
        process_name_equals = (params.get("process_name_equals") or "").strip() or None
        cmdline_contains = (params.get("cmdline_contains") or "").strip() or None
        parent_cmdline_contains = (params.get("parent_cmdline_contains") or "").strip() or None
        parent_name = (params.get("parent_name") or "").strip() or None
        parent_name_not_equals = (params.get("parent_name_not_equals") or "").strip() or None
        user_equals = (params.get("user") or "").strip() or None
        raw_hash = (params.get("hash") or "").strip() or None
        imagepath_contains = (params.get("imagepath_contains") or "").strip() or None
        execution_mode = (params.get("execution_mode") or "").strip() or None
        suspicious_reputation_only = bool(params.get("suspicious_reputation_only", False))
        started_after = (params.get("started_after") or "").strip() or None
        started_before = (params.get("started_before") or "").strip() or None
        hostname_contains = (params.get("hostname_contains") or "").strip() or None
        hostname_equals = (params.get("hostname_equals") or "").strip() or None
        include_host_info = bool(params.get("include_host_info", False))
        include_powershell_content = bool(params.get("include_powershell_content", False))

        if execution_mode and execution_mode not in Q.PROCESS_EXECUTION_MODES:
            return self._failure(
                "INVALID_INPUT",
                f"execution_mode must be one of {list(Q.PROCESS_EXECUTION_MODES)}.",
            )

        hash_type: Optional[Q.HashType] = None
        hash_value: Optional[str] = None
        if raw_hash:
            detected = Q.detect_hash_type(raw_hash)
            if detected is None:
                return self._failure(
                    "INVALID_HASH",
                    f"'hash' is not a valid MD5/SHA1/SHA256 hex value (got length={len(raw_hash)}).",
                )
            hash_type, hash_value = detected

        if parent_cmdline_contains and scope == "history":
            return self._failure(
                "INVALID_INPUT",
                "parent_cmdline_contains is only available with scope='running' "
                "(parent_cmdline does not exist on the ProcessHistory collector).",
            )

        max_rows = Q.clamp_max_rows(params.get("max_rows"))
        export_csv = bool(params.get("export_csv", False))
        export_json = bool(params.get("export_json", False))

        # Build a soft warning when include_host_info is true without a
        # restrictive filter — the caller may exceed API limits.
        warnings: list[str] = []
        narrow_filter_present = bool(
            hash_value
            or process_name_equals
            or hostname_equals
            or parent_cmdline_contains
            or cmdline_contains
        )
        if include_host_info and not narrow_filter_present:
            warnings.append(
                "include_host_info=True without a narrow filter (hash, "
                "process_name_equals, hostname_equals, cmdline_contains or "
                "parent_cmdline_contains) may exceed Trellix API limits and "
                "truncate results. Consider include_host_info=False for "
                "aggregated fleet-wide hunting."
            )

        collectors: list[Q.ProcessCollector]
        if scope == "running":
            collectors = ["Processes"]
        elif scope == "history":
            collectors = ["ProcessHistory"]
        else:
            collectors = ["Processes", "ProcessHistory"]

        all_rows: list[dict] = []
        query_ids: dict[str, str] = {}
        meta_combined: dict = {"total_count": 0, "total_hosts": 0}
        truncated_any = False

        try:
            async with Q.build_client(config) as client:
                for collector in collectors:
                    payload = Q.build_processes_payload(
                        collector=collector,
                        process_name_contains=process_name,
                        process_name_equals=process_name_equals,
                        cmdline_contains=cmdline_contains,
                        parent_cmdline_contains=(
                            parent_cmdline_contains if collector == "Processes" else None
                        ),
                        parent_name_equals=parent_name,
                        parent_name_not_equals=parent_name_not_equals,
                        user_equals=user_equals,
                        hash_type=hash_type,
                        hash_value=hash_value,
                        imagepath_contains=imagepath_contains,
                        execution_mode=execution_mode,
                        suspicious_reputation_only=suspicious_reputation_only,
                        started_after=started_after,
                        started_before=started_before,
                        hostname_contains=hostname_contains,
                        hostname_equals=hostname_equals,
                        include_host_info=include_host_info,
                        include_powershell_content=include_powershell_content,
                    )
                    qid, rows, meta, truncated = await Q.run_search_pipeline(
                        client,
                        api_version="v1",
                        payload=payload,
                        max_rows=max_rows,
                    )
                    query_ids[collector] = qid
                    all_rows.extend(_normalize_rows(rows, source=collector))
                    meta_combined["total_count"] += int(meta.get("total_count", len(rows)))
                    meta_combined["total_hosts"] = max(
                        meta_combined["total_hosts"], int(meta.get("total_hosts", 0))
                    )
                    truncated_any = truncated_any or truncated
        except TrellixEDRError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.status_code in (429, 500, 502, 503, 504)
            return self._failure(exc.code, str(exc), retryable=retryable, execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("trellix_edr_search_processes: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        # Trim merged rows to max_rows when scope='both'.
        if len(all_rows) > max_rows:
            all_rows = all_rows[:max_rows]
            truncated_any = True

        group_by = [
            "Processes_name",
            "Processes_sha256",
            "Processes_sha1",
            "Processes_md5",
            "Processes_user",
            "Processes_parentname",
            "Processes_process_reputation",
            "Processes_execution_mode",
        ]
        if include_host_info:
            group_by = ["HostInfo_hostname", "HostInfo_ip_address"] + group_by
        if scope == "both":
            group_by = ["source"] + group_by

        summary = Q.summarize(all_rows, group_by=group_by)

        artifacts_prefix = (
            f"trellix_edr_processes_{'_'.join(query_ids.values())}"
            if query_ids
            else "trellix_edr_processes"
        )
        artifacts = await maybe_export(
            self,
            rows=all_rows,
            export_csv=export_csv,
            export_json=export_json,
            filename_prefix=artifacts_prefix,
            agent_context=agent_context,
        )

        elapsed = int((time.monotonic() - t0) * 1000)
        result_data: dict = {
            "query_id": query_ids if scope == "both" else next(iter(query_ids.values()), None),
            "api_version": "v1",
            "scope": scope,
            "criteria": {
                "process_name": process_name,
                "process_name_equals": process_name_equals,
                "cmdline_contains": cmdline_contains,
                "parent_cmdline_contains": parent_cmdline_contains,
                "parent_name": parent_name,
                "parent_name_not_equals": parent_name_not_equals,
                "user": user_equals,
                "hash_type": hash_type,
                "hash_value": hash_value,
                "imagepath_contains": imagepath_contains,
                "execution_mode": execution_mode,
                "suspicious_reputation_only": suspicious_reputation_only,
                "started_after": started_after,
                "started_before": started_before,
                "hostname_contains": hostname_contains,
                "hostname_equals": hostname_equals,
                "include_host_info": include_host_info,
                "include_powershell_content": include_powershell_content,
            },
            "total_count": meta_combined["total_count"],
            "total_hosts": meta_combined["total_hosts"],
            "truncated": truncated_any,
            "summary": summary,
            "rows": all_rows,
            "artifacts": artifacts,
        }
        if warnings:
            result_data["warnings"] = warnings

        return self._success(result_data, execution_time_ms=elapsed)
