"""gSage AI — OPNsense write actions (audited, approval-gated).

Firewall response and change operations on an OPNsense firewall. Every
call requires a ``reason`` for the audit log and is subject to the
platform approval workflow (``requires_approval=True``).

Actions:

- ``block_ip``    — Add an IP to a firewall alias (the blocklist) and,
                  by default, drop the IP's active connection states so
                  the block takes effect immediately on existing flows.
- ``unblock_ip``  — Remove an IP from the alias.
- ``kill_states`` — Drop active firewall states matching an IP (best
                  effort; available on recent OPNsense).
- ``add_rule`` / ``toggle_rule`` / ``del_rule`` — Manage filter rules,
                  applying the change afterwards.
- ``ids_toggle_rule`` — Enable/disable a Suricata rule (by SID) and
                  reload the ruleset.
- ``restart_service`` — Restart an OPNsense service by name.

The blocklist alias must already exist in OPNsense and be referenced by a
block rule for ``block_ip`` to have effect. Permission: ``firewall:write``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.firewall.opnsense._client import (
    OPNSENSE_CONFIG_DEFAULTS,
    OPNSENSE_CONFIG_SCHEMA,
    OPNsenseClient,
    OPNsenseError,
    build_opnsense_client,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = frozenset({
    "block_ip",
    "unblock_ip",
    "kill_states",
    "add_rule",
    "toggle_rule",
    "del_rule",
    "ids_toggle_rule",
    "restart_service",
})

_RULE_ACTIONS = ("pass", "block", "reject")
_DIRECTIONS = ("in", "out")


class _ParamError(Exception):
    pass


def _require(params: dict, field: str) -> str:
    val = params.get(field)
    if isinstance(val, str):
        val = val.strip()
    if val in (None, ""):
        raise _ParamError(f"'{field}' is required for this action.")
    return str(val)


def _resolve_alias(client: OPNsenseClient, params: dict) -> str:
    alias = (params.get("alias") or "").strip() or client.block_alias
    if not alias:
        raise _ParamError(
            "alias is required (pass 'alias' or set 'block_alias' in the "
            "profile)."
        )
    return alias


class OPNsenseManageTool(BaseTool):
    """Approval-gated OPNsense firewall response & change operations.

    Every action requires a free-form ``reason`` for audit and is subject
    to the platform approval workflow.

    Permission: ``firewall:write``.
    """

    name: ClassVar[str] = "opnsense_manage"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Approval-gated OPNsense actions: block/unblock IP (+kill states), "
        "manage filter rules, toggle Suricata rules, restart services."
    )
    category: ClassVar[str] = "firewall"
    config_namespace: ClassVar[str] = "opnsense"
    permissions: ClassVar[list[str]] = ["firewall:write"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 120
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "target_entities": "ip",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action", "reason"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which write operation to perform.",
            },
            "profile": {"type": "string"},
            "reason": {
                "type": "string",
                "minLength": 5,
                "description": (
                    "Free-form justification recorded in the audit log."
                ),
            },
            "ip": {
                "type": "string",
                "description": (
                    "[block_ip, unblock_ip, kill_states] Target IPv4/IPv6 "
                    "address (or CIDR for alias actions)."
                ),
            },
            "alias": {
                "type": "string",
                "description": (
                    "[block_ip, unblock_ip] Firewall alias to modify. "
                    "Defaults to the profile's block_alias."
                ),
            },
            "kill_states": {
                "type": "boolean",
                "description": (
                    "[block_ip] Also drop the IP's active states so the "
                    "block applies to existing connections (default true)."
                ),
            },
            "uuid": {
                "type": "string",
                "description": "[toggle_rule, del_rule] Rule UUID.",
            },
            "enabled": {
                "type": "boolean",
                "description": (
                    "[toggle_rule, ids_toggle_rule] Desired enabled state."
                ),
            },
            "rule_action": {
                "type": "string",
                "enum": list(_RULE_ACTIONS),
                "description": "[add_rule] pass | block | reject.",
            },
            "interface": {
                "type": "string",
                "description": "[add_rule] Interface (e.g. 'wan', 'lan').",
            },
            "direction": {
                "type": "string",
                "enum": list(_DIRECTIONS),
                "description": "[add_rule] Traffic direction (default 'in').",
            },
            "protocol": {
                "type": "string",
                "description": "[add_rule] Protocol (e.g. 'any', 'TCP', 'UDP').",
            },
            "source_net": {
                "type": "string",
                "description": "[add_rule] Source (IP/CIDR/alias/'any').",
            },
            "destination_net": {
                "type": "string",
                "description": "[add_rule] Destination (IP/CIDR/alias/'any').",
            },
            "destination_port": {
                "type": "string",
                "description": "[add_rule] Destination port (e.g. '443').",
            },
            "description": {
                "type": "string",
                "description": "[add_rule] Rule description.",
            },
            "sid": {
                "type": "string",
                "description": "[ids_toggle_rule] Suricata signature ID (SID).",
            },
            "service": {
                "type": "string",
                "description": (
                    "[restart_service] OPNsense service name (e.g. 'suricata', "
                    "'unbound')."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = OPNSENSE_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = OPNSENSE_CONFIG_DEFAULTS
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
            async with build_opnsense_client(config) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(client, params, agent_context)
        except _ParamError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INVALID_PARAMS", str(exc), execution_time_ms=elapsed
            )
        except OPNsenseError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.code in ("CONNECTION_ERROR", "TIMEOUT", "RATE_LIMITED")
            return self._failure(
                exc.code, str(exc), retryable=retryable,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("opnsense_manage(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={"action": action, **data}, execution_time_ms=elapsed
        )

    # ── Alias helpers ────────────────────────────────────────────────────────

    @staticmethod
    async def _alias_util(
        client: OPNsenseClient, op: str, alias: str, address: str
    ) -> dict:
        """Run alias_util add/del and verify the 'status' field."""
        resp = await client.post(
            f"/firewall/alias_util/{op}/{alias}", {"address": address}
        )
        status = str((resp or {}).get("status") or "").lower()
        if "done" not in status and "ok" not in status:
            raise OPNsenseError(
                f"alias_util {op} did not confirm (status={status!r}, "
                f"alias={alias!r}). Does the alias exist?",
                code="INVALID_PARAMS",
            )
        return resp if isinstance(resp, dict) else {}

    @staticmethod
    async def _kill_states(client: OPNsenseClient, ip: str) -> dict:
        """Best-effort drop of states matching an IP. Returns a status dict.

        ``killStates`` exists on recent OPNsense; on older builds the
        endpoint is absent (NOT_FOUND) — we report that without failing the
        caller so a block is never silently undone by a states quirk.
        """
        try:
            resp = await client.post(
                "/diagnostics/firewall/killStates", {"filter": ip}
            )
            return {"killed": True, "detail": resp}
        except OPNsenseError as exc:
            log.info("kill_states(%s) unavailable/failed: %s", ip, exc)
            return {"killed": False, "error": exc.code, "detail": str(exc)}

    # ── Block / unblock ──────────────────────────────────────────────────────

    async def _do_block_ip(
        self, client: OPNsenseClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        ip = _require(params, "ip")
        alias = _resolve_alias(client, params)
        do_kill = params.get("kill_states")
        do_kill = True if do_kill is None else bool(do_kill)
        log.warning(
            "opnsense_manage block_ip ip=%s alias=%s reason=%r",
            ip, alias, params.get("reason"),
        )
        await self._alias_util(client, "add", alias, ip)
        states = await self._kill_states(client, ip) if do_kill else {"killed": False}
        return {"ip": ip, "alias": alias, "blocked": True, "states": states}

    async def _do_unblock_ip(
        self, client: OPNsenseClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        ip = _require(params, "ip")
        alias = _resolve_alias(client, params)
        log.warning(
            "opnsense_manage unblock_ip ip=%s alias=%s reason=%r",
            ip, alias, params.get("reason"),
        )
        await self._alias_util(client, "delete", alias, ip)
        return {"ip": ip, "alias": alias, "blocked": False}

    async def _do_kill_states(
        self, client: OPNsenseClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        ip = _require(params, "ip")
        log.warning(
            "opnsense_manage kill_states ip=%s reason=%r",
            ip, params.get("reason"),
        )
        states = await self._kill_states(client, ip)
        return {"ip": ip, "states": states}

    # ── Rules ────────────────────────────────────────────────────────────────

    async def _do_add_rule(
        self, client: OPNsenseClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        rule_action = (params.get("rule_action") or "").strip()
        if rule_action not in _RULE_ACTIONS:
            raise _ParamError(f"rule_action must be one of {list(_RULE_ACTIONS)}.")
        interface = _require(params, "interface")
        direction = (params.get("direction") or "in").strip()
        rule = {
            "enabled": "1",
            "action": rule_action,
            "interface": interface,
            "direction": direction,
            "protocol": (params.get("protocol") or "any").strip(),
            "source_net": (params.get("source_net") or "any").strip(),
            "destination_net": (params.get("destination_net") or "any").strip(),
            "destination_port": (params.get("destination_port") or "").strip(),
            "description": (params.get("description") or "gSage").strip(),
        }
        log.warning(
            "opnsense_manage add_rule action=%s if=%s src=%s dst=%s reason=%r",
            rule_action, interface, rule["source_net"], rule["destination_net"],
            params.get("reason"),
        )
        resp = await client.post("/firewall/filter/addRule", {"rule": rule})
        await client.apply_filter()
        return {"created": True, "uuid": (resp or {}).get("uuid"), "rule": rule}

    async def _do_toggle_rule(
        self, client: OPNsenseClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        uuid = _require(params, "uuid")
        enabled = params.get("enabled")
        if enabled is None:
            raise _ParamError("toggle_rule requires 'enabled' (true/false).")
        flag = "1" if enabled else "0"
        log.warning(
            "opnsense_manage toggle_rule uuid=%s enabled=%s reason=%r",
            uuid, enabled, params.get("reason"),
        )
        await client.post(f"/firewall/filter/toggleRule/{uuid}/{flag}")
        await client.apply_filter()
        return {"uuid": uuid, "enabled": bool(enabled)}

    async def _do_del_rule(
        self, client: OPNsenseClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        uuid = _require(params, "uuid")
        log.warning(
            "opnsense_manage del_rule uuid=%s reason=%r",
            uuid, params.get("reason"),
        )
        await client.post(f"/firewall/filter/delRule/{uuid}")
        await client.apply_filter()
        return {"uuid": uuid, "deleted": True}

    # ── IDS ──────────────────────────────────────────────────────────────────

    async def _do_ids_toggle_rule(
        self, client: OPNsenseClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        sid = _require(params, "sid")
        enabled = params.get("enabled")
        if enabled is None:
            raise _ParamError("ids_toggle_rule requires 'enabled' (true/false).")
        log.warning(
            "opnsense_manage ids_toggle_rule sid=%s enabled=%s reason=%r",
            sid, enabled, params.get("reason"),
        )
        await client.post(
            "/ids/settings/setRule",
            {"sid": sid, "enabled": "1" if enabled else "0"},
        )
        await client.post("/ids/service/reloadRules")
        return {"sid": sid, "enabled": bool(enabled)}

    # ── Services ─────────────────────────────────────────────────────────────

    async def _do_restart_service(
        self, client: OPNsenseClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        service = _require(params, "service")
        log.warning(
            "opnsense_manage restart_service service=%s reason=%r",
            service, params.get("reason"),
        )
        resp = await client.post(f"/core/service/restart/{service}")
        return {"service": service, "restarted": True, "detail": resp}


_ = Any
