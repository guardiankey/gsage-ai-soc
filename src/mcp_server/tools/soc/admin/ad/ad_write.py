"""gSage AI — ad_write tool (Active Directory write/mutate).

Consolidates every write/mutation operation against Active Directory.
Writes are NEVER executed over LDAP — they run as PowerShell scripts on
a configured Windows jump host through an SSH transport.

Supported actions:

* ``disable_user``           — Disable-ADAccount (+ optional move to
  quarantine OU).
* ``enable_user``            — Enable-ADAccount.
* ``unlock_user``            — Unlock-ADAccount.
* ``reset_password``         — Set-ADAccountPassword -Reset (random pwd).
* ``force_password_change``  — Set-ADUser -ChangePasswordAtLogon $true.
* ``create_user``            — New-ADUser (+ optional group membership).
* ``modify_group_membership`` — Add/Remove-ADGroupMember.

Permission: ``ad:write``.  Every action requires human approval and is
gated by ``config.write_enabled`` + the ``protected_users`` /
``protected_groups`` safety lists.

Audit
-----
* ``reset_password`` returns the plaintext password in
  ``data.password`` for the operator, but we also set
  ``data._audit_redact_fields=["password"]`` as a hint to downstream
  audit redaction (the plaintext must NEVER be persisted).
* ``audit_field_mapping`` maps the target DN as the audit entity.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.soc.admin.ad import _pwsh_templates as tpl
from src.mcp_server.tools.soc.admin.ad._pwsh_runner import (
    PwshConnectionConfig,
    PwshError,
    run_pwsh_script,
)
from src.mcp_server.tools.soc.admin.ad._safety import (
    AdWriteBlocked,
    assert_group_not_protected,
    assert_user_not_protected,
    assert_write_enabled,
    generate_password,
)
from src.mcp_server.tools.soc.admin.ad._schemas import (
    AD_CONFIG_DEFAULTS,
    AD_CONFIG_SCHEMA,
    AD_WRITE_PARAMS_SCHEMA,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)


def _require_param(params: dict, key: str, action: str) -> str:
    val = params.get(key)
    if not val or not isinstance(val, str) or not val.strip():
        raise _ParamError(f"Action '{action}' requires parameter '{key}'.")
    return val.strip()


class _ParamError(Exception):
    """Raised when required per-action params are missing."""


class AdWriteTool(BaseTool):
    """Write operations against Active Directory via pwsh-over-SSH.

    All actions require ``ad:write`` permission AND human approval.
    Fail-closed when ``config.write_enabled`` is not true.

    Permission: ``ad:write``.
    """

    name: ClassVar[str] = "ad_write"
    config_namespace: ClassVar[str] = "active_directory"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Active Directory write operations: disable/enable/unlock users, "
        "reset passwords, create users, manage group membership."
    )
    category: ClassVar[str] = "admin"
    permissions: ClassVar[list[str]] = ["ad:write"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 120
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True

    config_schema: ClassVar[Optional[dict]] = AD_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = AD_CONFIG_DEFAULTS
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {"target_entities": "user_dn"}
    audit_output: ClassVar[bool] = True

    params_schema: ClassVar[Optional[dict]] = AD_WRITE_PARAMS_SCHEMA

    # ------------------------------------------------------------------

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        start = time.perf_counter()
        action = params.get("action")

        if not action:
            return self._failure(
                "INVALID_PARAMS",
                "Missing required parameter 'action'.",
                retryable=False,
                execution_time_ms=int((time.perf_counter() - start) * 1000),
            )

        # ── Safety gate: write_enabled ───────────────────────────────
        try:
            assert_write_enabled(config)
        except AdWriteBlocked as blk:
            return self._failure(
                blk.code,
                blk.message,
                retryable=False,
                execution_time_ms=int((time.perf_counter() - start) * 1000),
            )

        # ── Build PwshConnectionConfig (sanity-check jump host config) ─
        try:
            ssh_config = PwshConnectionConfig.from_tool_config(config)
        except PwshError as exc:
            return self._failure(
                exc.code,
                exc.message,
                retryable=False,
                execution_time_ms=int((time.perf_counter() - start) * 1000),
            )

        approval_summary = str(params.get("_approval_summary") or "").strip() or action
        log_in_description = bool(config.get("log_actions_in_description", False))

        try:
            dispatch = self._dispatch_map()
            handler = dispatch.get(action)
            if handler is None:
                return self._failure(
                    "INVALID_PARAMS",
                    f"Unsupported action '{action}'.",
                    retryable=False,
                    execution_time_ms=int((time.perf_counter() - start) * 1000),
                )
            data = await handler(
                params=params,
                config=config,
                ssh_config=ssh_config,
                approval_summary=approval_summary,
                log_in_description=log_in_description,
            )
        except _ParamError as exc:
            return self._failure(
                "INVALID_PARAMS",
                str(exc),
                retryable=False,
                execution_time_ms=int((time.perf_counter() - start) * 1000),
            )
        except AdWriteBlocked as blk:
            return self._failure(
                blk.code,
                blk.message,
                retryable=False,
                execution_time_ms=int((time.perf_counter() - start) * 1000),
            )
        except PwshError as exc:
            log.info("ad_write %s failed: %s — %s", action, exc.code, exc.message)
            retryable = exc.code in {"SSH_FAILED", "PWSH_TIMEOUT"}
            return self._failure(
                exc.code,
                exc.message,
                retryable=retryable,
                execution_time_ms=int((time.perf_counter() - start) * 1000),
            )
        except Exception as exc:  # pragma: no cover
            log.exception("ad_write %s raised unexpected error", action)
            return self._failure(
                "UNEXPECTED",
                f"Unexpected error: {exc}",
                retryable=False,
                execution_time_ms=int((time.perf_counter() - start) * 1000),
            )

        return self._success(
            data={"action": action, **data},
            execution_time_ms=int((time.perf_counter() - start) * 1000),
        )

    # ── Dispatch table ──────────────────────────────────────────────

    def _dispatch_map(self) -> dict:
        return {
            "disable_user": self._do_disable_user,
            "enable_user": self._do_enable_user,
            "unlock_user": self._do_unlock_user,
            "reset_password": self._do_reset_password,
            "force_password_change": self._do_force_password_change,
            "create_user": self._do_create_user,
            "modify_group_membership": self._do_modify_group_membership,
        }

    # ── Action handlers ──────────────────────────────────────────────

    async def _do_disable_user(
        self,
        *,
        params: dict,
        config: dict,
        ssh_config: PwshConnectionConfig,
        approval_summary: str,
        log_in_description: bool,
    ) -> dict:
        user_dn = _require_param(params, "user_dn", "disable_user")
        assert_user_not_protected(
            user_dn=user_dn,
            sam_account_name=None,
            config=config,
        )
        move = bool(params.get("move_to_quarantine", True))
        quarantine = (config.get("quarantine_ou") or "").strip() if move else ""
        script = tpl.disable_user_script(
            user_dn=user_dn,
            quarantine_ou=quarantine or None,
            log_in_description=log_in_description,
            action_summary=approval_summary,
        )
        res = await run_pwsh_script(config=ssh_config, script=script)
        return {"user_dn": user_dn, "moved_to": quarantine or None, "pwsh": res.data}

    async def _do_enable_user(
        self,
        *,
        params: dict,
        config: dict,
        ssh_config: PwshConnectionConfig,
        approval_summary: str,
        log_in_description: bool,
    ) -> dict:
        user_dn = _require_param(params, "user_dn", "enable_user")
        assert_user_not_protected(
            user_dn=user_dn, sam_account_name=None, config=config,
        )
        script = tpl.enable_user_script(
            user_dn=user_dn,
            log_in_description=log_in_description,
            action_summary=approval_summary,
        )
        res = await run_pwsh_script(config=ssh_config, script=script)
        return {"user_dn": user_dn, "pwsh": res.data}

    async def _do_unlock_user(
        self,
        *,
        params: dict,
        config: dict,
        ssh_config: PwshConnectionConfig,
        approval_summary: str,
        log_in_description: bool,
    ) -> dict:
        user_dn = _require_param(params, "user_dn", "unlock_user")
        assert_user_not_protected(
            user_dn=user_dn, sam_account_name=None, config=config,
        )
        script = tpl.unlock_user_script(
            user_dn=user_dn,
            log_in_description=log_in_description,
            action_summary=approval_summary,
        )
        res = await run_pwsh_script(config=ssh_config, script=script)
        return {"user_dn": user_dn, "pwsh": res.data}

    async def _do_reset_password(
        self,
        *,
        params: dict,
        config: dict,
        ssh_config: PwshConnectionConfig,
        approval_summary: str,
        log_in_description: bool,
    ) -> dict:
        user_dn = _require_param(params, "user_dn", "reset_password")
        assert_user_not_protected(
            user_dn=user_dn, sam_account_name=None, config=config,
        )
        length = int(
            params.get("length")
            or config.get("password_policy_length", 16)
            or 16
        )
        new_password = generate_password(length=length)
        script = tpl.reset_password_script(
            user_dn=user_dn,
            new_password=new_password,
            log_in_description=log_in_description,
            action_summary=approval_summary,
        )
        res = await run_pwsh_script(config=ssh_config, script=script)
        # The plaintext password is returned to the operator but MUST NOT be
        # stored in audit logs. Downstream audit redaction honours the hint.
        return {
            "user_dn": user_dn,
            "password": new_password,
            "password_length": length,
            "_audit_redact_fields": ["password"],
            "pwsh": res.data,
        }

    async def _do_force_password_change(
        self,
        *,
        params: dict,
        config: dict,
        ssh_config: PwshConnectionConfig,
        approval_summary: str,
        log_in_description: bool,
    ) -> dict:
        user_dn = _require_param(params, "user_dn", "force_password_change")
        assert_user_not_protected(
            user_dn=user_dn, sam_account_name=None, config=config,
        )
        script = tpl.force_password_change_script(
            user_dn=user_dn,
            log_in_description=log_in_description,
            action_summary=approval_summary,
        )
        res = await run_pwsh_script(config=ssh_config, script=script)
        return {"user_dn": user_dn, "pwsh": res.data}

    async def _do_create_user(
        self,
        *,
        params: dict,
        config: dict,
        ssh_config: PwshConnectionConfig,
        approval_summary: str,
        log_in_description: bool,
    ) -> dict:
        sam = _require_param(params, "sam_account_name", "create_user")
        display = _require_param(params, "display_name", "create_user")
        ou = _require_param(params, "ou_dn", "create_user")

        # The new-user DN doesn't exist yet — match the protected list
        # against sAMAccountName only.
        assert_user_not_protected(
            user_dn=None, sam_account_name=sam, config=config,
        )
        # Block creation directly inside a protected OU whose DN matches
        # a protected_groups glob (defensive; most orgs won't set this).
        groups: list[str] = list(params.get("groups") or [])
        for g in groups:
            assert_group_not_protected(
                group_dn=g if g.upper().startswith("CN=") or g.upper().startswith("OU=") else None,
                group_name=None if g.upper().startswith("CN=") or g.upper().startswith("OU=") else g,
                config=config,
            )

        length = int(config.get("password_policy_length", 16) or 16)
        initial_password = str(params.get("initial_password") or "").strip()
        generated = False
        if not initial_password:
            initial_password = generate_password(length=length)
            generated = True

        script = tpl.create_user_script(
            sam_account_name=sam,
            display_name=display,
            given_name=params.get("given_name") or None,
            surname=params.get("surname") or None,
            ou_dn=ou,
            user_principal_name=params.get("user_principal_name") or None,
            initial_password=initial_password,
            enabled=bool(params.get("enabled", False)),
            groups=groups,
            log_in_description=log_in_description,
            action_summary=approval_summary,
        )
        res = await run_pwsh_script(config=ssh_config, script=script)
        out: dict[str, Any] = {
            "sam_account_name": sam,
            "display_name": display,
            "ou_dn": ou,
            "enabled": bool(params.get("enabled", False)),
            "groups": groups,
            "pwsh": res.data,
        }
        if generated:
            out["password"] = initial_password
            out["_audit_redact_fields"] = ["password"]
        return out

    async def _do_modify_group_membership(
        self,
        *,
        params: dict,
        config: dict,
        ssh_config: PwshConnectionConfig,
        approval_summary: str,
        log_in_description: bool,
    ) -> dict:
        group_dn = _require_param(params, "group_dn", "modify_group_membership")
        user_dn = _require_param(params, "user_dn", "modify_group_membership")
        operation = _require_param(params, "operation", "modify_group_membership")
        if operation not in ("add", "remove"):
            raise _ParamError(
                "modify_group_membership: 'operation' must be 'add' or 'remove'."
            )
        assert_group_not_protected(
            group_dn=group_dn, group_name=None, config=config,
        )
        assert_user_not_protected(
            user_dn=user_dn, sam_account_name=None, config=config,
        )
        script = tpl.modify_group_membership_script(
            group_dn=group_dn,
            user_dn=user_dn,
            operation=operation,
            log_in_description=log_in_description,
            action_summary=approval_summary,
        )
        res = await run_pwsh_script(config=ssh_config, script=script)
        return {
            "group_dn": group_dn,
            "user_dn": user_dn,
            "operation": operation,
            "pwsh": res.data,
        }
