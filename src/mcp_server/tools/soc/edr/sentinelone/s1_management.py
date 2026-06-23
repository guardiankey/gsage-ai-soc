"""gSage AI — SentinelOne response / write actions (approval-gated).

All write operations on SentinelOne agents and threats. Every call is
gated by the platform HITL approval workflow (``requires_approval=True``);
the framework-injected ``_approval_summary`` carries the analyst's
justification into the audit log.

Actions:

- ``isolate_agent``        — Network-isolate an agent (disconnect).
- ``reconnect_agent``      — Re-connect a previously isolated agent.
- ``scan_agent``           — Trigger a full disk scan.
- ``mitigate_threat``      — Apply a mitigation: kill | quarantine |
                           remediate | rollback-remediation | un-quarantine.
- ``set_threat_verdict``   — Set the analyst verdict (true_positive /
                           false_positive / suspicious / undefined).
- ``add_threat_note``      — Attach an analyst note to a threat.
- ``add_to_blocklist``     — Add a file hash (SHA1) to the blocklist.
- ``remove_from_blocklist`` — Remove a blocklist restriction by id.

An agent is addressed by ``agent_id`` or ``computer_name`` (must be
unique). Permission: ``sentinelone:write``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.edr.sentinelone._client import (
    S1_CONFIG_DEFAULTS,
    S1_CONFIG_SCHEMA,
    SentinelOneClient,
    SentinelOneError,
    build_s1_client,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = frozenset({
    "isolate_agent",
    "reconnect_agent",
    "scan_agent",
    "mitigate_threat",
    "set_threat_verdict",
    "add_threat_note",
    "add_to_blocklist",
    "remove_from_blocklist",
})

_MITIGATIONS = (
    "kill", "quarantine", "remediate", "rollback-remediation", "un-quarantine",
)
_VERDICTS = ("true_positive", "false_positive", "suspicious", "undefined")
_OS_TYPES = ("windows", "linux", "macos")


class _ParamError(Exception):
    pass


def _require(params: dict, field: str) -> str:
    val = params.get(field)
    if isinstance(val, str):
        val = val.strip()
    if val in (None, ""):
        raise _ParamError(f"'{field}' is required for this action.")
    return str(val)


async def _resolve_agent_id(client: SentinelOneClient, params: dict) -> str:
    """Resolve an agent UUID from ``agent_id`` or ``computer_name``."""
    aid = (params.get("agent_id") or "").strip()
    if aid:
        return aid
    name = (params.get("computer_name") or "").strip()
    if not name:
        raise _ParamError("This action requires 'agent_id' or 'computer_name'.")
    rows = await client.paginate(
        "/agents", {"computerName__contains": name}, limit=10, max_items=10
    )
    if not rows:
        raise SentinelOneError(f"No agent matched {name!r}.", code="NOT_FOUND")
    if len(rows) > 1:
        names = ", ".join(str(r.get("computerName")) for r in rows[:5])
        raise SentinelOneError(
            f"computer_name {name!r} matched multiple agents ({names}); "
            "use agent_id.",
            code="CONFLICT",
        )
    return str(rows[0].get("id"))


def _affected(resp: dict) -> Optional[int]:
    """Extract the 'affected' count S1 returns for bulk actions."""
    data = resp.get("data")
    if isinstance(data, dict):
        return data.get("affected")
    return None


class S1ManagementTool(BaseTool):
    """Approval-gated SentinelOne response actions (agents + threats).

    Every action is subject to the platform approval workflow; the
    ``_approval_summary`` justification is recorded in the audit log.

    Permission: ``sentinelone:write``.
    """

    name: ClassVar[str] = "s1_management"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Approval-gated SentinelOne response: isolate/reconnect/scan agents, "
        "mitigate threats, set verdict, notes, hash blocklist."
    )
    category: ClassVar[str] = "edr"
    config_namespace: ClassVar[str] = "sentinelone"
    permissions: ClassVar[list[str]] = ["sentinelone:write"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 120
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which write operation to perform.",
            },
            "profile": {"type": "string"},
            "agent_id": {
                "type": "string",
                "description": (
                    "[isolate_agent, reconnect_agent, scan_agent] Agent UUID."
                ),
            },
            "computer_name": {
                "type": "string",
                "description": (
                    "[isolate_agent, reconnect_agent, scan_agent] Resolve "
                    "agent by hostname (must be unique). Alternative to "
                    "agent_id."
                ),
            },
            "threat_id": {
                "type": "string",
                "description": (
                    "[mitigate_threat, set_threat_verdict, add_threat_note] "
                    "Threat ID."
                ),
            },
            "mitigation": {
                "type": "string",
                "enum": list(_MITIGATIONS),
                "description": "[mitigate_threat] Mitigation action to apply.",
            },
            "verdict": {
                "type": "string",
                "enum": list(_VERDICTS),
                "description": "[set_threat_verdict] Analyst verdict to set.",
            },
            "note": {
                "type": "string",
                "description": "[add_threat_note] Note text.",
            },
            "hash_sha1": {
                "type": "string",
                "description": "[add_to_blocklist] SHA1 of the file to block.",
            },
            "os_type": {
                "type": "string",
                "enum": list(_OS_TYPES),
                "description": "[add_to_blocklist] OS the blocklist entry applies to.",
            },
            "description": {
                "type": "string",
                "description": "[add_to_blocklist] Description for the blocklist entry.",
            },
            "restriction_id": {
                "type": "string",
                "description": "[remove_from_blocklist] Blocklist restriction id.",
            },
            "site_ids": {
                "type": "string",
                "description": (
                    "[add_to_blocklist] Comma-separated site IDs to scope the "
                    "entry. Defaults to the profile's default_site_ids; if "
                    "none, the entry is created at tenant (global) scope."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = S1_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = S1_CONFIG_DEFAULTS
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Execute ─────────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        action = (params.get("action") or "").strip()
        if action not in _ACTIONS:
            return self._failure(
                "INVALID_PARAMS",
                f"action must be one of {sorted(_ACTIONS)}; got {action!r}.",
            )
        try:
            async with build_s1_client(config) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(client, params, agent_context)
        except _ParamError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INVALID_PARAMS", str(exc), execution_time_ms=elapsed)
        except SentinelOneError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.code in ("CONNECTION_ERROR", "TIMEOUT", "RATE_LIMITED")
            return self._failure(
                exc.code, str(exc), retryable=retryable, execution_time_ms=elapsed
            )
        except Exception as exc:
            log.exception("s1_management(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INTERNAL_ERROR", str(exc), execution_time_ms=elapsed)

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(data={"action": action, **data}, execution_time_ms=elapsed)

    # ── Agent actions ────────────────────────────────────────────────────────

    async def _agent_action(
        self, client: SentinelOneClient, params: dict, endpoint: str, label: str,
    ) -> dict:
        aid = await _resolve_agent_id(client, params)
        log.warning("s1_management %s agent=%s", label, aid)
        resp = await client.post(
            f"/agents/actions/{endpoint}", {"filter": {"ids": [aid]}}
        )
        return {"agent_id": aid, "operation": label, "affected": _affected(resp)}

    async def _do_isolate_agent(self, client, params, agent_context) -> dict:
        return await self._agent_action(client, params, "disconnect", "isolate")

    async def _do_reconnect_agent(self, client, params, agent_context) -> dict:
        return await self._agent_action(client, params, "connect", "reconnect")

    async def _do_scan_agent(self, client, params, agent_context) -> dict:
        return await self._agent_action(client, params, "initiateScan", "scan")

    # ── Threat actions ───────────────────────────────────────────────────────

    async def _do_mitigate_threat(
        self, client: SentinelOneClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        tid = _require(params, "threat_id")
        action = _require(params, "mitigation")
        if action not in _MITIGATIONS:
            raise _ParamError(f"mitigation must be one of {list(_MITIGATIONS)}.")
        log.warning("s1_management mitigate_threat threat=%s action=%s", tid, action)
        resp = await client.post(
            f"/threats/mitigate/{action}", {"filter": {"ids": [tid]}}
        )
        return {"threat_id": tid, "mitigation": action, "affected": _affected(resp)}

    async def _do_set_threat_verdict(
        self, client: SentinelOneClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        tid = _require(params, "threat_id")
        verdict = _require(params, "verdict")
        if verdict not in _VERDICTS:
            raise _ParamError(f"verdict must be one of {list(_VERDICTS)}.")
        log.warning("s1_management set_verdict threat=%s verdict=%s", tid, verdict)
        resp = await client.post(
            "/threats/analyst-verdict",
            {"filter": {"ids": [tid]}, "data": {"analystVerdict": verdict}},
        )
        return {"threat_id": tid, "verdict": verdict, "affected": _affected(resp)}

    async def _do_add_threat_note(
        self, client: SentinelOneClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        tid = _require(params, "threat_id")
        note = _require(params, "note")
        log.warning("s1_management add_note threat=%s", tid)
        await client.post(
            "/threats/notes",
            {"filter": {"ids": [tid]}, "data": {"text": note}},
        )
        return {"threat_id": tid, "note_added": True}

    # ── Blocklist ────────────────────────────────────────────────────────────

    async def _do_add_to_blocklist(
        self, client: SentinelOneClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        sha1 = _require(params, "hash_sha1")
        os_type = _require(params, "os_type")
        if os_type not in _OS_TYPES:
            raise _ParamError(f"os_type must be one of {list(_OS_TYPES)}.")
        description = (params.get("description") or "gSage blocklist").strip()
        sites = client.resolve_site_ids(params)
        filt: dict = {"siteIds": sites} if sites else {"tenant": True}
        log.warning("s1_management add_to_blocklist sha1=%s os=%s", sha1, os_type)
        resp = await client.post(
            "/restrictions",
            {
                "filter": filt,
                "data": {
                    "type": "black_hash",
                    "value": sha1,
                    "osType": os_type,
                    "description": description,
                    "source": "gSage",
                },
            },
        )
        return {
            "hash_sha1": sha1, "os_type": os_type, "scope": filt,
            "blocked": True, "detail": resp.get("data"),
        }

    async def _do_remove_from_blocklist(
        self, client: SentinelOneClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        rid = _require(params, "restriction_id")
        log.warning("s1_management remove_from_blocklist id=%s", rid)
        resp = await client.delete(
            "/restrictions", {"data": {"type": "black_hash", "ids": [rid]}}
        )
        return {"restriction_id": rid, "removed": True, "affected": _affected(resp)}


_ = Any
