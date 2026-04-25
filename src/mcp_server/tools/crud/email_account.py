"""gSage AI — Email Account CRUD tool.

Allows the AI agent to manage IMAP/SMTP email account configurations.

    list    — list email accounts for current org              (requires crud:email_account:read)
    get     — get details of a specific email account          (requires crud:email_account:read)
    create  — configure a new email account                    (requires crud:email_account:write)
    update  — update non-secret fields                         (requires crud:email_account:write)
    delete  — soft-delete an email account                     (requires crud:email_account:write)

Safety rules:
    - Passwords (imap_password, smtp_password) are NEVER exposed or accepted as input.
    - To change passwords, use the administration interface directly.
"""

from __future__ import annotations

import time
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import ToolResult
from src.mcp_server.tools.crud_base import CrudBaseTool
from src.shared.models.email_account import GSageEmailAccount
from src.shared.security.context import AgentContext

_PERM_READ = "crud:email_account:read"
_PERM_WRITE = "crud:email_account:write"

_SAFE_UPDATE_FIELDS = {
    "display_name", "is_active",
    "imap_host", "imap_port", "imap_use_tls", "imap_username",
    "imap_folder", "imap_idle_supported",
    "smtp_host", "smtp_port", "smtp_use_tls", "smtp_username",
}

_REQUIRED_CREATE_FIELDS = {
    "email", "display_name",
    "imap_host", "imap_username",
    "smtp_host", "smtp_username",
}


def _serialize(acc: GSageEmailAccount) -> dict:
    return {
        "id": str(acc.id),
        "org_id": str(acc.org_id),
        "email": acc.email,
        "display_name": acc.display_name,
        "is_active": acc.is_active,
        "imap_host": acc.imap_host,
        "imap_port": acc.imap_port,
        "imap_use_tls": acc.imap_use_tls,
        "imap_username": acc.imap_username,
        "imap_folder": acc.imap_folder,
        "imap_idle_supported": acc.imap_idle_supported,
        "smtp_host": acc.smtp_host,
        "smtp_port": acc.smtp_port,
        "smtp_use_tls": acc.smtp_use_tls,
        "smtp_username": acc.smtp_username,
        "created_at": acc.created_at.isoformat(),
    }


class EmailAccountCrudTool(CrudBaseTool):
    """CRUD tool for GSageEmailAccount (no password exposure)."""

    name: ClassVar[str] = "email_account"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Configure email account integrations (IMAP/SMTP) for the organization's send_email tool"
    category: ClassVar[str] = "crud"
    available: ClassVar[bool] = False  # temporarily disabled — UX still maturing
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 15

    valid_actions: ClassVar[frozenset[str]] = frozenset({"list", "get", "create", "update", "delete"})
    write_actions: ClassVar[frozenset[str]] = frozenset({"create", "update", "delete"})
    write_permission: ClassVar[str] = _PERM_WRITE

    permissions: ClassVar[list[str]] = [_PERM_READ, _PERM_WRITE]

    config_schema: ClassVar[None] = None
    config_defaults: ClassVar[dict] = {}
    state_schema: ClassVar[None] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get", "create", "update", "delete"],
                "description": (
                    "list: list all email accounts in the org. "
                    "get: get details of a specific account. "
                    "create: add a new email account (passwords set separately). "
                    "update: modify non-secret fields. "
                    "delete: soft-delete an email account."
                ),
            },
            "account_id": {
                "type": "string",
                "description": "[get/update/delete] Email account UUID.",
            },
            "email": {"type": "string", "description": "[create] Mailbox email address."},
            "display_name": {"type": "string", "description": "[create/update] Friendly name."},
            "is_active": {"type": "boolean", "description": "[update] Enable or disable account."},
            "imap_host": {"type": "string", "description": "[create/update] IMAP server hostname."},
            "imap_port": {"type": "integer", "description": "[create/update] IMAP port (default 993)."},
            "imap_use_tls": {"type": "boolean", "description": "[create/update] Use TLS for IMAP."},
            "imap_username": {"type": "string", "description": "[create/update] IMAP username."},
            "imap_folder": {"type": "string", "description": "[create/update] IMAP folder (default INBOX)."},
            "imap_idle_supported": {"type": "boolean", "description": "[create/update] Server supports IMAP IDLE."},
            "smtp_host": {"type": "string", "description": "[create/update] SMTP server hostname."},
            "smtp_port": {"type": "integer", "description": "[create/update] SMTP port (default 587)."},
            "smtp_use_tls": {"type": "boolean", "description": "[create/update] Use TLS for SMTP."},
            "smtp_username": {"type": "string", "description": "[create/update] SMTP username."},
        },
    }

    # ── Handlers ─────────────────────────────────────────────────────────────

    async def _handle_list(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        result = await session.execute(
            select(GSageEmailAccount)
            .where(GSageEmailAccount.org_id == agent_context.org_id)
            .order_by(GSageEmailAccount.display_name)
        )
        accounts = result.scalars().all()
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"accounts": [_serialize(a) for a in accounts], "count": len(accounts)},
            execution_time_ms=elapsed,
        )

    async def _handle_get(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        account_id = params.get("account_id", "")
        if not account_id:
            return self._failure(code="INVALID_PARAMS", message="'account_id' is required.")
        result = await session.execute(
            select(GSageEmailAccount).where(
                GSageEmailAccount.id == account_id,
                GSageEmailAccount.org_id == agent_context.org_id,
            )
        )
        acc = result.scalar_one_or_none()
        if not acc:
            return self._failure(code="NOT_FOUND", message=f"Email account '{account_id}' not found.")
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(acc), execution_time_ms=elapsed)

    async def _handle_create(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        missing = _REQUIRED_CREATE_FIELDS - set(params)
        if missing:
            return self._failure(
                code="INVALID_PARAMS",
                message=f"Required fields missing: {', '.join(sorted(missing))}.",
            )

        # Passwords must be set via admin interface — use a placeholder that forces
        # reconfiguration. The email worker rejects accounts with placeholder passwords.
        _PLACEHOLDER = b"\x00"

        acc = GSageEmailAccount(
            org_id=agent_context.org_id,
            email=params["email"],
            display_name=params["display_name"],
            imap_host=params["imap_host"],
            imap_port=int(params.get("imap_port", 993)),
            imap_use_tls=bool(params.get("imap_use_tls", True)),
            imap_username=params["imap_username"],
            imap_folder=params.get("imap_folder", "INBOX"),
            imap_idle_supported=bool(params.get("imap_idle_supported", True)),
            smtp_host=params["smtp_host"],
            smtp_port=int(params.get("smtp_port", 587)),
            smtp_use_tls=bool(params.get("smtp_use_tls", True)),
            smtp_username=params["smtp_username"],
            _imap_password_encrypted=_PLACEHOLDER,
            _smtp_password_encrypted=_PLACEHOLDER,
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={
                **_serialize(acc),
                "notice": "Passwords must be configured via the administration interface before this account becomes active.",
            },
            execution_time_ms=elapsed,
        )

    async def _handle_update(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        account_id = params.get("account_id", "")
        if not account_id:
            return self._failure(code="INVALID_PARAMS", message="'account_id' is required.")

        update_fields = {k: v for k, v in params.items() if k in _SAFE_UPDATE_FIELDS}
        if not update_fields:
            return self._failure(
                code="INVALID_PARAMS",
                message=f"No updatable fields provided. Allowed: {', '.join(sorted(_SAFE_UPDATE_FIELDS))}.",
            )

        result = await session.execute(
            select(GSageEmailAccount).where(
                GSageEmailAccount.id == account_id,
                GSageEmailAccount.org_id == agent_context.org_id,
            )
        )
        acc = result.scalar_one_or_none()
        if not acc:
            return self._failure(code="NOT_FOUND", message=f"Email account '{account_id}' not found.")

        for key, value in update_fields.items():
            setattr(acc, key, value)
        await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(acc), execution_time_ms=elapsed)

    async def _handle_delete(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        account_id = params.get("account_id", "")
        if not account_id:
            return self._failure(code="INVALID_PARAMS", message="'account_id' is required.")

        result = await session.execute(
            select(GSageEmailAccount).where(
                GSageEmailAccount.id == account_id,
                GSageEmailAccount.org_id == agent_context.org_id,
            )
        )
        acc = result.scalar_one_or_none()
        if not acc:
            return self._failure(code="NOT_FOUND", message=f"Email account '{account_id}' not found.")

        acc.is_active = False
        await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"id": account_id, "deleted": True, "email": acc.email},
            execution_time_ms=elapsed,
        )
