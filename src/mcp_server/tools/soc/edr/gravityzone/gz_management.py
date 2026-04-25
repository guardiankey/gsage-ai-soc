"""gSage AI — GravityZone Management (write) tool.

Consolidates ALL write/mutation operations for BitDefender GravityZone.
Every action requires ``gravityzone:write`` permission and human-in-the-loop
approval.

Supported actions:
    add_to_blocklist       — Add hash/path/connection rules to the Blocklist (API v1.2)
    remove_from_blocklist  — Remove entries from the Blocklist (API v1.2)
    isolate_endpoint       — Isolate an endpoint from the network (API v1.1)
    restore_isolation      — Restore a previously isolated endpoint (API v1.1)
    change_incident_status — Change the status of a specific incident (API v1.0)
    update_incident_note   — Add or update a note on an incident (API v1.1)

Required permission: ``gravityzone:write``
All actions require human approval.
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.edr.gravityzone._client import GravityZoneClient, GravityZoneError
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_GZ_CONFIG_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "api_key": {
            "type": "string",
            "description": "GravityZone API key.",
            "sensitive": True,
        },
        "base_url": {
            "type": "string",
            "description": "GravityZone API base URL (default: cloud endpoint).",
        },
    },
    "additionalProperties": False,
}
_GZ_CONFIG_DEFAULTS: dict = {
    "base_url": "https://cloud.gravityzone.bitdefender.com/api",
}

# Incident status mapping (string enum → API int)
_INCIDENT_STATUSES: dict[str, int] = {
    "open": 1,
    "investigating": 2,
    "closed": 3,
    "false_positive": 4,
}


class GzManagementTool(BaseTool):
    """Execute all write/mutation operations in BitDefender GravityZone.

    All actions require ``gravityzone:write`` permission and human-in-the-loop
    approval.

    **Blocklist actions:**

    - ``add_to_blocklist`` — Add hash, path, or connection rules to the
      GravityZone Blocklist (API v1.2, all rule types supported).
      Requires ``blocklist_type`` and ``rules``.
    - ``remove_from_blocklist`` — Remove one or more entries from the Blocklist
      (API v1.2).  Requires ``blocklist_item_ids`` (array of entry IDs; obtain
      from gz_security action=blocklist_items).

    **Endpoint isolation actions:**

    - ``isolate_endpoint`` — Isolate an endpoint from network communication
      (API v1.1, returns task ID array).  Requires ``endpoint_id``.
    - ``restore_isolation`` — Restore a previously isolated endpoint
      (API v1.1, returns task ID array).  Requires ``endpoint_id``.

    **Incident management actions:**

    - ``change_incident_status`` — Change the status of an incident.
      Requires ``incident_type``, ``incident_id``, and ``status``.
    - ``update_incident_note`` — Add or update a note on an incident
      (API v1.1, max 50 000 characters).
      Requires ``incident_type``, ``incident_id``, and ``note``.

    Permission: ``gravityzone:write``
    """

    name: ClassVar[str] = "gz_management"
    version: ClassVar[str] = "2.0.0"
    summary: ClassVar[str] = "Execute write operations in GravityZone: isolate/restore endpoints, update blocklist entries"
    category: ClassVar[str] = "edr"
    permissions: ClassVar[list[str]] = ["gravityzone:write"]
    rate_limit_per_minute: ClassVar[int] = 10
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {"target_entities": "endpoint_id"}
    audit_output: ClassVar[bool] = True

    config_schema: ClassVar[Optional[dict]] = _GZ_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = _GZ_CONFIG_DEFAULTS
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
                    "add_to_blocklist",
                    "remove_from_blocklist",
                    "isolate_endpoint",
                    "restore_isolation",
                    "change_incident_status",
                    "update_incident_note",
                ],
                "description": (
                    "Write operation to perform:\n"
                    "\n"
                    "Blocklist:\n"
                    "- add_to_blocklist: add hash/path/connection rules (requires blocklist_type, rules)\n"
                    "- remove_from_blocklist: remove entries by ID (requires blocklist_item_ids)\n"
                    "\n"
                    "Endpoint isolation:\n"
                    "- isolate_endpoint: isolate endpoint from network (requires endpoint_id)\n"
                    "- restore_isolation: restore isolated endpoint (requires endpoint_id)\n"
                    "\n"
                    "Incidents:\n"
                    "- change_incident_status: update status (requires incident_type, incident_id, status)\n"
                    "- update_incident_note: add/update note (requires incident_type, incident_id, note)"
                ),
            },
            # ── Endpoint isolation ─────────────────────────────────────────
            "endpoint_id": {
                "type": "string",
                "description": (
                    "Endpoint ID. Required for isolate_endpoint and restore_isolation."
                ),
            },
            # ── Blocklist ──────────────────────────────────────────────────
            "blocklist_type": {
                "type": "string",
                "enum": ["hash", "path", "connection"],
                "description": (
                    "Type of blocklist rule to add. Required for add_to_blocklist.\n"
                    "- hash: block by file hash (MD5 or SHA256)\n"
                    "- path: block by file path\n"
                    "- connection: block by network connection details"
                ),
            },
            "rules": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "Array of rule objects to add to the blocklist (add_to_blocklist).\n"
                    "\n"
                    "For blocklist_type=hash, each rule requires:\n"
                    "  { \"algorithm\": \"sha256\" | \"md5\", \"hash\": \"<hex>\", \"note\": \"<reason>\" }\n"
                    "\n"
                    "For blocklist_type=path, each rule requires:\n"
                    "  { \"path\": \"<file_path>\", \"note\": \"<reason>\" }\n"
                    "\n"
                    "For blocklist_type=connection, each rule requires:\n"
                    "  { \"rule_name\": \"<name>\", \"command_line\": \"<cmd>\",\n"
                    "    \"protocol\": \"<tcp|udp>\", \"direction\": \"<in|out|both>\",\n"
                    "    \"ip_version\": \"<4|6>\", \"local_address\": \"<cidr>\",\n"
                    "    \"remote_address\": \"<cidr>\", \"operating_system\": \"<os>\" }"
                ),
                "items": {"type": "object"},
            },
            "recursive": {
                "type": "boolean",
                "default": True,
                "description": (
                    "Apply blocklist rule recursively to sub-companies "
                    "(add_to_blocklist, default: true)."
                ),
            },
            "blocklist_item_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "Array of blocklist entry IDs to remove. "
                    "Required for remove_from_blocklist. "
                    "Obtain IDs from gz_security action=blocklist_items."
                ),
            },
            # ── Incident management ────────────────────────────────────────
            "incident_type": {
                "type": "string",
                "enum": ["incidents", "extendedIncidents"],
                "description": (
                    "Incident API namespace. Required for change_incident_status "
                    "and update_incident_note.\n"
                    "- incidents: standard security incidents\n"
                    "- extendedIncidents: XDR/advanced incidents"
                ),
            },
            "incident_id": {
                "type": "string",
                "description": (
                    "Incident ID. Required for change_incident_status "
                    "and update_incident_note."
                ),
            },
            "status": {
                "type": "string",
                "enum": ["open", "investigating", "closed", "false_positive"],
                "description": (
                    "New incident status. Required for change_incident_status."
                ),
            },
            "note": {
                "type": "string",
                "maxLength": 50000,
                "description": (
                    "Note text to attach to the incident (max 50 000 chars). "
                    "Required for update_incident_note."
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

        if not agent_context.has_permission("gravityzone:write"):
            return self._failure(
                "PERMISSION_DENIED",
                "This tool requires the 'gravityzone:write' permission.",
            )

        try:
            async with GravityZoneClient(
                api_key=config.get("api_key") or None,
                base_url=config.get("base_url") or None,
            ) as client:
                if action == "add_to_blocklist":
                    result = await self._add_to_blocklist(client, params)
                elif action == "remove_from_blocklist":
                    result = await self._remove_from_blocklist(client, params)
                elif action == "isolate_endpoint":
                    result = await self._isolate_endpoint(client, params)
                elif action == "restore_isolation":
                    result = await self._restore_isolation(client, params)
                elif action == "change_incident_status":
                    result = await self._change_incident_status(client, params)
                elif action == "update_incident_note":
                    result = await self._update_incident_note(client, params)
                else:
                    return self._failure("INVALID_ACTION", f"Unknown action: {action}")
        except GravityZoneError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.status_code in (429, 500, 502, 503, 504)
            return self._failure(
                f"GZ_ERROR_{exc.code}" if exc.code else "GZ_ERROR",
                str(exc),
                retryable=retryable,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("gz_management: unexpected error (action=%s)", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(result, execution_time_ms=elapsed)

    # ── Blocklist handlers ─────────────────────────────────────────────────

    async def _add_to_blocklist(self, client: GravityZoneClient, params: dict) -> dict:
        blocklist_type = params.get("blocklist_type", "").strip()
        rules = params.get("rules")
        if not blocklist_type or not rules:
            raise GravityZoneError(
                "blocklist_type and rules are required for add_to_blocklist.", code=-32602
            )

        # Build rule objects according to the v1.2 API structure
        built_rules: list[dict] = []
        for rule in rules:
            if blocklist_type == "hash":
                built_rules.append({
                    "details": {
                        "algorithm": rule.get("algorithm", "sha256"),
                        "hash": rule.get("hash", ""),
                        "note": rule.get("note", ""),
                    }
                })
            elif blocklist_type == "path":
                built_rules.append({
                    "details": {
                        "path": rule.get("path", ""),
                        "note": rule.get("note", ""),
                    }
                })
            elif blocklist_type == "connection":
                built_rules.append({
                    "details": {
                        "ruleName": rule.get("rule_name", ""),
                        "commandLine": rule.get("command_line", ""),
                        "protocol": rule.get("protocol", ""),
                        "direction": rule.get("direction", ""),
                        "ipVersion": rule.get("ip_version", "4"),
                        "localAddress": rule.get("local_address", ""),
                        "remoteAddress": rule.get("remote_address", ""),
                        "operatingSystem": rule.get("operating_system", ""),
                    }
                })

        rpc_params: dict = {
            "blocklistType": blocklist_type,
            "rules": built_rules,
            "recursive": params.get("recursive", True),
        }
        result = await client.call(
            "incidents", "addToBlocklist", rpc_params, api_version="v1.2"
        )
        return {
            "action": "add_to_blocklist",
            "blocklist_type": blocklist_type,
            "rules_count": len(built_rules),
            "result": result,
        }

    async def _remove_from_blocklist(self, client: GravityZoneClient, params: dict) -> dict:
        item_ids = params.get("blocklist_item_ids")
        if not item_ids or not isinstance(item_ids, list):
            raise GravityZoneError(
                "blocklist_item_ids (non-empty list) is required for remove_from_blocklist.",
                code=-32602,
            )
        result = await client.call(
            "incidents",
            "removeFromBlocklist",
            {"ids": item_ids},
            api_version="v1.2",
        )
        return {
            "action": "remove_from_blocklist",
            "removed_count": len(item_ids),
            "result": result,
        }

    # ── Endpoint isolation handlers ────────────────────────────────────────

    async def _isolate_endpoint(self, client: GravityZoneClient, params: dict) -> dict:
        endpoint_id = params.get("endpoint_id", "").strip()
        if not endpoint_id:
            raise GravityZoneError("endpoint_id is required.", code=-32602)
        # v1.1: returns array of task IDs
        result = await client.call(
            "incidents",
            "createIsolateEndpointTask",
            {"endpointId": endpoint_id},
            api_version="v1.1",
        )
        return {
            "action": "isolate_endpoint",
            "endpoint_id": endpoint_id,
            "task_ids": result if isinstance(result, list) else [result],
        }

    async def _restore_isolation(self, client: GravityZoneClient, params: dict) -> dict:
        endpoint_id = params.get("endpoint_id", "").strip()
        if not endpoint_id:
            raise GravityZoneError("endpoint_id is required.", code=-32602)
        # v1.1: returns array of task IDs
        result = await client.call(
            "incidents",
            "createRestoreEndpointFromIsolationTask",
            {"endpointId": endpoint_id},
            api_version="v1.1",
        )
        return {
            "action": "restore_isolation",
            "endpoint_id": endpoint_id,
            "task_ids": result if isinstance(result, list) else [result],
        }

    # ── Incident management handlers ───────────────────────────────────────

    async def _change_incident_status(self, client: GravityZoneClient, params: dict) -> dict:
        incident_type = params.get("incident_type", "").strip()
        incident_id = params.get("incident_id", "").strip()
        status_str = params.get("status", "").strip()
        if not incident_type or not incident_id or not status_str:
            raise GravityZoneError(
                "incident_type, incident_id, and status are required.", code=-32602
            )
        status_int = _INCIDENT_STATUSES.get(status_str)
        if status_int is None:
            raise GravityZoneError(
                f"Invalid status '{status_str}'. "
                f"Allowed: {', '.join(_INCIDENT_STATUSES.keys())}.",
                code=-32602,
            )
        result = await client.call(
            incident_type,
            "changeIncidentStatus",
            {"incidentId": incident_id, "status": status_int},
        )
        return {
            "action": "change_incident_status",
            "incident_type": incident_type,
            "incident_id": incident_id,
            "status": status_str,
            "result": result,
        }

    async def _update_incident_note(self, client: GravityZoneClient, params: dict) -> dict:
        incident_type = params.get("incident_type", "").strip()
        incident_id = params.get("incident_id", "").strip()
        note = params.get("note", "")
        if not incident_type or not incident_id or not note:
            raise GravityZoneError(
                "incident_type, incident_id, and note are required.", code=-32602
            )
        # v1.1: returns Boolean true directly (no wrapper object)
        result = await client.call(
            incident_type,
            "updateIncidentNote",
            {"incidentId": incident_id, "note": note},
            api_version="v1.1",
        )
        return {
            "action": "update_incident_note",
            "incident_type": incident_type,
            "incident_id": incident_id,
            "success": bool(result),
        }
