"""gSage AI — Trellix EDR host quarantine status tool.

Read-only lookup of host info matching a hostname or IP, useful to confirm
whether a host is currently online/managed and as a complement to
:class:`trellix_edr_quarantine_host`.

Permission: ``edr:read``
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.edr.trellix import _query as Q
from src.mcp_server.tools.soc.edr.trellix._client import TrellixEDRError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_STATUS_MAX_ROWS = 50


class TrellixEdrGetHostQuarantineStatusTool(BaseTool):
    """Return host metadata for hosts matching a hostname/IP.

    Uses the v2 HostInfo projection.  Accepts partial matches (CONTAINS) and
    returns up to 50 candidate hosts, so it can be used both as a sanity
    check before quarantine and as a quick host inventory lookup.

    Note: the explicit ``is_quarantined`` flag depends on tenant-version
    fields which are not always exposed via the public API.  When available,
    it is surfaced under each host as ``is_quarantined``; otherwise the
    field is ``null`` and the agent should rely on
    ``trellix_edr_quarantine_host`` audit logs.

    Permission: ``edr:read``
    """

    name: ClassVar[str] = "trellix_edr_get_host_quarantine_status"
    config_namespace: ClassVar[str] = "trellix_edr"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Look up Trellix EDR hosts by hostname/IP and return their quarantine "
        "and metadata status"
    )
    category: ClassVar[str] = "edr"
    permissions: ClassVar[list[str]] = ["edr:read"]

    rate_limit_per_minute: ClassVar[int] = 30
    timeout_seconds: ClassVar[int] = 600
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
        "target_host": "hostname",
        "target_ip": "ip_address",
    }
    audit_output: ClassVar[bool] = False

    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "hostname": {
                "type": "string",
                "description": (
                    "Hostname filter.  Substring match (CONTAINS) by default."
                ),
            },
            "ip_address": {
                "type": "string",
                "description": "IP address filter.  Substring match (CONTAINS).",
            },
            "exact": {
                "type": "boolean",
                "default": False,
                "description": (
                    "When true, use EQUALS instead of CONTAINS for the filter."
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
        hostname = (params.get("hostname") or "").strip() or None
        ip_address = (params.get("ip_address") or "").strip() or None
        exact = bool(params.get("exact", False))

        if not hostname and not ip_address:
            return self._failure(
                "INVALID_INPUT",
                "Provide at least one of 'hostname' or 'ip_address'.",
            )

        try:
            locator_query = Q.build_host_locator_query(
                hostname=hostname,
                ip_address=ip_address,
                exact=exact,
            )
            async with Q.build_client(config) as client:
                query_id, rows, _meta, _truncated = await Q.run_search_pipeline(
                    client,
                    api_version="v2",
                    query=locator_query,
                    max_rows=_STATUS_MAX_ROWS,
                )
        except TrellixEDRError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.status_code in (429, 500, 502, 503, 504)
            return self._failure(exc.code, str(exc), retryable=retryable, execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("trellix_edr_get_host_quarantine_status: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        hosts = []
        for r in rows:
            hosts.append(
                {
                    "system_id": r.get("system_id"),
                    "hostname": r.get("HostInfo_hostname"),
                    "ip_address": r.get("HostInfo_ip_address"),
                    "platform": r.get("HostInfo_platform"),
                    "os": r.get("HostInfo_os"),
                    "connection_status": r.get("HostInfo_connection_status"),
                    "is_quarantined": _extract_quarantine_flag(r),
                    "raw": r,
                }
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            {
                "query_id": query_id,
                "api_version": "v2",
                "criteria": {
                    "hostname": hostname,
                    "ip_address": ip_address,
                    "exact": exact,
                },
                "match_count": len(hosts),
                "hosts": hosts,
            },
            execution_time_ms=elapsed,
        )


def _extract_quarantine_flag(row: dict) -> Optional[bool]:
    """Best-effort extraction of a quarantine flag from HostInfo fields.

    The primary signal in Trellix EDR v2 is ``HostInfo.connection_status``,
    which reports values like ``connected``, ``disconnected`` and
    ``contained`` (containment = network isolation = quarantine).
    Falls back to legacy ``isolated``/``quarantine_status`` style fields.

    Returns ``True``/``False`` when a known field is parsable; ``None``
    when no recognised field is exposed.
    """
    cs = row.get("HostInfo_connection_status")
    if isinstance(cs, str) and cs.strip():
        normalized = cs.strip().lower()
        if normalized in ("contained", "isolated", "quarantined"):
            return True
        if normalized in ("connected", "disconnected", "online", "offline"):
            return False

    for key in (
        "HostInfo_is_isolated",
        "HostInfo_isolated",
        "HostInfo_quarantine_status",
        "HostInfo_os_isolated",
    ):
        if key in row and row[key] is not None:
            value = row[key]
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                return value.strip().lower() in ("true", "1", "yes", "isolated", "quarantined", "contained")
    return None
