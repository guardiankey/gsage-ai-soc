"""gSage AI — Trellix EDR host quarantine tool.

Searches for the target host by hostname or IP, requires a unique result,
then triggers ``quarantineHost`` (or ``unquarantineHost``) via the v1
remediation API.

Permission: ``edr:write``, ``edr:quarantine``
Always background · Requires approval.
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

# Cap on the locator search to keep the candidate list reviewable.
_LOCATOR_MAX_ROWS = 50


class TrellixEdrQuarantineHostTool(BaseTool):
    """Quarantine (or release) a host on Trellix EDR by hostname or IP.

    The tool first searches Trellix to locate the matching host, requires
    exactly one match, then triggers the ``quarantineHost`` (or
    ``unquarantineHost``) remediation against that host's ``system_id``.

    Errors:
        - ``HOST_NOT_FOUND``    — the locator search returned zero hosts.
        - ``AMBIGUOUS_TARGET``  — the locator search returned more than one
          host; the candidates are listed in the error data.

    Permission: ``edr:write``, ``edr:quarantine``
    """

    name: ClassVar[str] = "trellix_edr_quarantine_host"
    config_namespace: ClassVar[str] = "trellix_edr"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Quarantine or release a host on Trellix EDR by hostname/IP "
        "(requires approval)"
    )
    category: ClassVar[str] = "edr"
    permissions: ClassVar[list[str]] = ["edr:write", "edr:quarantine"]

    rate_limit_per_minute: ClassVar[int] = 5
    timeout_seconds: ClassVar[int] = 900
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True
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
        "unquarantine": "unquarantine",
        "reason": "reason",
    }
    audit_output: ClassVar[bool] = True

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["reason"],
        "properties": {
            "hostname": {
                "type": "string",
                "description": (
                    "Target host name (exact match preferred). "
                    "Provide either 'hostname' or 'ip_address'."
                ),
            },
            "ip_address": {
                "type": "string",
                "description": (
                    "Target host IP address (exact match preferred). "
                    "Provide either 'hostname' or 'ip_address'."
                ),
            },
            "unquarantine": {
                "type": "boolean",
                "default": False,
                "description": (
                    "When true, releases the host from quarantine "
                    "instead of isolating it."
                ),
            },
            "reason": {
                "type": "string",
                "minLength": 5,
                "description": (
                    "Reason justifying the action (recorded in the audit log)."
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
        unquarantine = bool(params.get("unquarantine", False))
        reason = (params.get("reason") or "").strip()

        if bool(hostname) == bool(ip_address):
            return self._failure(
                "INVALID_INPUT",
                "Provide exactly one of 'hostname' or 'ip_address'.",
            )
        if not reason:
            return self._failure("INVALID_INPUT", "'reason' is required.")

        action = "unquarantineHost" if unquarantine else "quarantineHost"
        locator_query = Q.build_host_locator_query(
            hostname=hostname,
            ip_address=ip_address,
            exact=True,
        )

        try:
            async with Q.build_client(config) as client:
                query_id, rows, _meta, _truncated = await Q.run_search_pipeline(
                    client,
                    api_version="v2",
                    query=locator_query,
                    max_rows=_LOCATOR_MAX_ROWS,
                )

                hosts = _unique_hosts(rows)
                if not hosts:
                    elapsed = int((time.monotonic() - t0) * 1000)
                    return self._failure(
                        "HOST_NOT_FOUND",
                        f"No host matched (hostname={hostname!r}, ip_address={ip_address!r}).",
                        execution_time_ms=elapsed,
                    )
                if len(hosts) > 1:
                    elapsed = int((time.monotonic() - t0) * 1000)
                    return self._failure(
                        "AMBIGUOUS_TARGET",
                        (
                            f"More than one host matched ({len(hosts)} candidates). "
                            "Refine the input or use the unique system_id directly. "
                            f"Candidates: {hosts[:10]}"
                        ),
                        execution_time_ms=elapsed,
                    )

                target = hosts[0]
                system_id = target["system_id"]
                reaction_id = await client.start_remediation(
                    action=action,
                    query_id=query_id,
                    row_ids=[system_id],
                )
        except TrellixEDRError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.status_code in (429, 500, 502, 503, 504)
            return self._failure(exc.code, str(exc), retryable=retryable, execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("trellix_edr_quarantine_host: unexpected error")
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            {
                "action": action,
                "hostname": target.get("hostname"),
                "ip_address": target.get("ip_address"),
                "system_id": target["system_id"],
                "search_id": query_id,
                "reaction_id": reaction_id,
                "reason": reason,
                "status": "submitted",
            },
            execution_time_ms=elapsed,
        )


def _unique_hosts(rows: list[dict]) -> list[dict]:
    """Collapse v2 result rows into a unique host list keyed by system_id.

    v2 results expose the host's system_id as the row ``id`` (lifted to
    ``system_id`` by the flattener).  When the locator search returns
    multiple matching hosts, each gets one row.
    """
    seen: dict[str, dict] = {}
    for r in rows:
        sid = r.get("system_id")
        if not sid:
            continue
        host = r.get("HostInfo_hostname") or r.get("hostname")
        ip = r.get("HostInfo_ip_address") or r.get("ip_address")
        if sid in seen:
            continue
        seen[sid] = {
            "system_id": sid,
            "hostname": host,
            "ip_address": ip,
        }
    return list(seen.values())
