"""gSage AI — Permission CRUD tool.

Allows the AI agent to manage the global permission tags used by the RBAC system.

    list    — list all permissions                             (requires crud:permission:read)
    get     — get details of a specific permission             (requires crud:permission:read)
    create  — create a new permission tag                      (requires crud:permission:write)
    delete  — delete a permission tag                          (requires crud:permission:write)

Notes:
    - Permissions are global (not org-scoped).
    - Deleting a permission automatically unassigns it from all groups (CASCADE).
"""

from __future__ import annotations

import time
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import ToolResult
from src.mcp_server.tools.crud_base import CrudBaseTool
from src.shared.models.permission import GSagePermission
from src.shared.security.context import AgentContext

_PERM_READ = "crud:permission:read"
_PERM_WRITE = "crud:permission:write"


def _serialize(perm: GSagePermission) -> dict:
    return {
        "id": str(perm.id),
        "tag": perm.tag,
        "description": perm.description,
        "category": perm.category,
        "created_at": perm.created_at.isoformat(),
    }


class PermissionCrudTool(CrudBaseTool):
    """CRUD tool for GSagePermission (global tag-based RBAC)."""

    name: ClassVar[str] = "permission"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Manage global permission tags and role-based access control (RBAC) entries"
    category: ClassVar[str] = "crud"
    available: ClassVar[bool] = False  # temporarily disabled — UX still maturing
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 15

    valid_actions: ClassVar[frozenset[str]] = frozenset({"list", "get", "create", "delete"})
    write_actions: ClassVar[frozenset[str]] = frozenset({"create", "delete"})
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
                "enum": ["list", "get", "create", "delete"],
                "description": (
                    "list: list all permission tags. "
                    "get: get details of a permission by tag or id. "
                    "create: create a new permission tag. "
                    "delete: delete a permission tag (unassigns from all groups)."
                ),
            },
            "permission_id": {"type": "string", "description": "[get/delete] Permission UUID."},
            "tag": {"type": "string", "description": "[get/create] Permission tag (e.g., dns:read)."},
            "description": {"type": "string", "description": "[create] Human-readable description."},
            "category": {
                "type": "string",
                "description": "[create] Category for grouping (e.g., tool, admin, network). Default: tool.",
            },
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
            select(GSagePermission).order_by(GSagePermission.category, GSagePermission.tag)
        )
        perms = result.scalars().all()
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"permissions": [_serialize(p) for p in perms], "count": len(perms)},
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
        tag = params.get("tag", "").strip()
        perm_id = params.get("permission_id", "")
        if not tag and not perm_id:
            return self._failure(code="INVALID_PARAMS", message="'tag' or 'permission_id' is required.")

        stmt = select(GSagePermission)
        if tag:
            stmt = stmt.where(GSagePermission.tag == tag)
        else:
            stmt = stmt.where(GSagePermission.id == perm_id)

        result = await session.execute(stmt)
        perm = result.scalar_one_or_none()
        if not perm:
            ref = tag or perm_id
            return self._failure(code="NOT_FOUND", message=f"Permission '{ref}' not found.")

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(perm), execution_time_ms=elapsed)

    async def _handle_create(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        tag = params.get("tag", "").strip()
        if not tag:
            return self._failure(code="INVALID_PARAMS", message="'tag' is required.")

        # Check for duplicates
        existing = await session.execute(
            select(GSagePermission).where(GSagePermission.tag == tag)
        )
        if existing.scalar_one_or_none():
            return self._failure(
                code="CONFLICT", message=f"Permission tag '{tag}' already exists."
            )

        perm = GSagePermission(
            tag=tag,
            description=params.get("description"),
            category=params.get("category", "tool"),
        )
        session.add(perm)
        await session.commit()
        await session.refresh(perm)

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(perm), execution_time_ms=elapsed)

    async def _handle_delete(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        tag = params.get("tag", "").strip()
        perm_id = params.get("permission_id", "")
        if not tag and not perm_id:
            return self._failure(code="INVALID_PARAMS", message="'tag' or 'permission_id' is required.")

        stmt = select(GSagePermission)
        if tag:
            stmt = stmt.where(GSagePermission.tag == tag)
        else:
            stmt = stmt.where(GSagePermission.id == perm_id)

        result = await session.execute(stmt)
        perm = result.scalar_one_or_none()
        ref = tag or perm_id
        if not perm:
            return self._failure(code="NOT_FOUND", message=f"Permission '{ref}' not found.")

        await session.delete(perm)
        await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"tag": perm.tag, "id": str(perm.id), "deleted": True},
            execution_time_ms=elapsed,
        )
