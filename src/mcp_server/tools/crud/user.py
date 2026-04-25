"""gSage AI — User CRUD tool (read-only).

Allows the AI agent to look up users within the organization.

    list    — list users in current org                        (requires crud:user:read)
    get     — get a specific user's details                    (requires crud:user:read)

Write operations (create / update / delete) are intentionally disabled —
user provisioning should go through the admin console or the dedicated
authentication endpoints. The write handlers remain in the code for
future re-enablement; they are simply not advertised in ``valid_actions``.

Safety rules:
    - Password hash is NEVER exposed.

Data model:
    GSageUser is globally unique by email.  Organization membership is
    represented via GSageUserOrganization (N:N with role).  Queries in
    this tool always scope through that join table using ``agent_context.org_id``.
"""

from __future__ import annotations

import time
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.mcp_server.tools.base import ToolResult
from src.mcp_server.tools.crud_base import CrudBaseTool
from src.shared.models.user import GSageUser
from src.shared.models.user_organization import GSageUserOrganization
from src.shared.security.context import AgentContext

_PERM_READ = "crud:user:read"
_PERM_WRITE = "crud:user:write"

_SAFE_UPDATE_FIELDS = {"full_name", "is_active", "secondary_emails"}
_REQUIRED_CREATE_FIELDS = {"email", "full_name"}

# Placeholder prevents login until the user sets a real password via reset flow.
_PLACEHOLDER_PASSWORD_HASH = "PLACEHOLDER_MUST_RESET"


def _serialize(user: GSageUser, membership: GSageUserOrganization | None = None) -> dict:
    data: dict = {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "is_active": user.is_active,
        "secondary_emails": user.secondary_emails,
        "created_at": user.created_at.isoformat(),
    }
    if membership is not None:
        data["role"] = membership.role
        data["org_id"] = str(membership.org_id)
        data["membership_active"] = membership.is_active
    return data


def _org_members_query(org_id):
    """Select users that belong to *org_id* via the membership join table."""
    return (
        select(GSageUser, GSageUserOrganization)
        .join(
            GSageUserOrganization,
            GSageUserOrganization.user_id == GSageUser.id,
        )
        .where(GSageUserOrganization.org_id == org_id)
    )


class UserCrudTool(CrudBaseTool):
    """CRUD tool for GSageUser (no password exposure)."""

    name: ClassVar[str] = "user"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Read-only lookup of gSage user accounts (list/get)"
    category: ClassVar[str] = "crud"
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 15

    # Read-only: write actions are intentionally omitted. Handlers for
    # create/update/delete remain below but are unreachable via dispatch.
    valid_actions: ClassVar[frozenset[str]] = frozenset({"list", "get"})
    write_actions: ClassVar[frozenset[str]] = frozenset()
    write_permission: ClassVar[str] = _PERM_WRITE

    permissions: ClassVar[list[str]] = [_PERM_READ]

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
                "enum": ["list", "get"],
                "description": (
                    "list: list all users in the org. "
                    "get: get user details."
                ),
            },
            "user_id": {"type": "string", "description": "[get] User UUID."},
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
        stmt = (
            _org_members_query(agent_context.org_id)
            .where(GSageUserOrganization.is_active.is_(True))
            .order_by(GSageUser.full_name)
        )
        result = await session.execute(stmt)
        rows = result.all()
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={
                "users": [_serialize(user, mem) for user, mem in rows],
                "count": len(rows),
            },
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
        user_id = params.get("user_id", "")
        if not user_id:
            return self._failure(code="INVALID_PARAMS", message="'user_id' is required.")
        stmt = _org_members_query(agent_context.org_id).where(GSageUser.id == user_id)
        result = await session.execute(stmt)
        row = result.one_or_none()
        if not row:
            return self._failure(code="NOT_FOUND", message=f"User '{user_id}' not found in this organization.")
        user, mem = row
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(user, mem), execution_time_ms=elapsed)

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

        # Check for duplicate email
        existing = await session.execute(
            select(GSageUser).where(GSageUser.email == params["email"])
        )
        if existing.scalar_one_or_none():
            return self._failure(
                code="DUPLICATE",
                message=f"A user with email '{params['email']}' already exists.",
            )

        user = GSageUser(
            email=params["email"],
            full_name=params["full_name"],
            password_hash=_PLACEHOLDER_PASSWORD_HASH,
            is_active=True,
        )
        session.add(user)
        await session.flush()  # get user.id before creating membership

        role = params.get("role", "member")
        membership = GSageUserOrganization(
            user_id=user.id,
            org_id=agent_context.org_id,
            role=role,
            is_active=True,
        )
        session.add(membership)
        await session.commit()
        await session.refresh(user)

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={
                **_serialize(user, membership),
                "notice": "User created with a placeholder password. The user must complete a password reset before logging in.",
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
        user_id = params.get("user_id", "")
        if not user_id:
            return self._failure(code="INVALID_PARAMS", message="'user_id' is required.")

        stmt = _org_members_query(agent_context.org_id).where(GSageUser.id == user_id)
        result = await session.execute(stmt)
        row = result.one_or_none()
        if not row:
            return self._failure(code="NOT_FOUND", message=f"User '{user_id}' not found in this organization.")
        user, mem = row

        # Update user-level fields
        user_updates = {k: v for k, v in params.items() if k in _SAFE_UPDATE_FIELDS}

        # Update org-level role
        new_role = params.get("role")
        if not user_updates and not new_role:
            allowed = sorted(_SAFE_UPDATE_FIELDS | {"role"})
            return self._failure(
                code="INVALID_PARAMS",
                message=f"No updatable fields provided. Allowed: {', '.join(allowed)}.",
            )

        for key, value in user_updates.items():
            setattr(user, key, value)
        if new_role:
            mem.role = new_role
        await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(user, mem), execution_time_ms=elapsed)

    async def _handle_delete(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        user_id = params.get("user_id", "")
        if not user_id:
            return self._failure(code="INVALID_PARAMS", message="'user_id' is required.")

        if str(agent_context.user_id) == str(user_id):
            return self._failure(
                code="FORBIDDEN",
                message="Cannot delete your own user account.",
            )

        stmt = _org_members_query(agent_context.org_id).where(GSageUser.id == user_id)
        result = await session.execute(stmt)
        row = result.one_or_none()
        if not row:
            return self._failure(code="NOT_FOUND", message=f"User '{user_id}' not found in this organization.")
        user, mem = row

        # Soft-delete: deactivate the membership (user account stays intact for other orgs)
        mem.is_active = False
        await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"id": user_id, "deleted": True, "email": user.email},
            execution_time_ms=elapsed,
        )
