"""gSage AI — Group CRUD tool.

Allows the AI agent to manage RBAC groups and their permission assignments.

    list            — list groups in current org               (requires crud:group:read)
    get             — get group details + its permissions       (requires crud:group:read)
    create          — create a new group                       (requires crud:group:write)
    update          — rename group or change description        (requires crud:group:write)
    delete          — soft-delete a group                       (requires crud:group:write)
    add_permission  — assign a permission tag to a group       (requires crud:group:write)
    remove_permission — remove a permission tag from a group   (requires crud:group:write)

Safety rules:
    - Cannot delete a group the current user belongs to.
"""

from __future__ import annotations

import uuid
import time
from typing import ClassVar

from sqlalchemy import delete, insert, or_, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import ToolResult
from src.mcp_server.tools.crud_base import CrudBaseTool
from src.shared.models.group import GSageGroup, gsage_group_permissions
from src.shared.models.permission import GSagePermission
from src.shared.models.user import GSageUser
from src.shared.security.context import AgentContext

_PERM_READ = "crud:group:read"
_PERM_WRITE = "crud:group:write"


def _serialize(group: GSageGroup, dept_perms: list[dict] | None = None) -> dict:
    data: dict = {
        "id": str(group.id),
        "org_id": str(group.org_id),
        "name": group.name,
        "description": group.description,
        "created_at": group.created_at.isoformat(),
    }
    # Include permissions with dept_id if provided via direct SQL query
    if dept_perms is not None:
        data["permissions"] = dept_perms
    elif "permissions" in group.__dict__:
        # Fallback: ORM-loaded (no dept_id info)
        data["permissions"] = [{"tag": p.tag, "dept_id": None} for p in group.permissions]
    return data


class GroupCrudTool(CrudBaseTool):
    """CRUD tool for GSageGroup with permission management."""

    name: ClassVar[str] = "group"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Manage organization user groups: create, read, update, delete, and assign permissions"
    category: ClassVar[str] = "crud"
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 15

    valid_actions: ClassVar[frozenset[str]] = frozenset({
        "list", "get", "create", "update", "delete",
        "add_permission", "remove_permission",
    })
    write_actions: ClassVar[frozenset[str]] = frozenset({
        "create", "update", "delete", "add_permission", "remove_permission",
    })
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
                "enum": ["list", "get", "create", "update", "delete", "add_permission", "remove_permission"],
                "description": (
                    "list: list all groups. "
                    "get: get group details with permissions. "
                    "create: create a new group. "
                    "update: rename or change description. "
                    "delete: delete a group (not one you belong to). "
                    "add_permission: assign a permission tag to the group. "
                    "remove_permission: unassign a permission tag from the group."
                ),
            },
            "group_id": {"type": "string", "description": "[get/update/delete/add_permission/remove_permission] Group UUID."},
            "name": {"type": "string", "description": "[create/update] Group name."},
            "description": {"type": "string", "description": "[create/update] Group description."},
            "permission_tag": {"type": "string", "description": "[add_permission/remove_permission] Permission tag (e.g., dns:read)."},
            "dept_id": {
                "type": "string",
                "description": "[add_permission/remove_permission] Optional department UUID to scope the permission. Omit or null for global (all departments).",
            },
        },
    }

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _load_user_group_ids(self, agent_context: AgentContext, session: AsyncSession) -> set[str]:
        """Return the set of group IDs the current user belongs to."""
        result = await session.execute(
            select(GSageUser)
            .options(selectinload(GSageUser.groups))
            .where(GSageUser.id == agent_context.user_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            return set()
        return {str(g.id) for g in user.groups}

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
            select(GSageGroup)
            .where(GSageGroup.org_id == agent_context.org_id)
            .order_by(GSageGroup.name)
        )
        groups = result.scalars().all()
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"groups": [_serialize(g) for g in groups], "count": len(groups)},
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
        group_id = params.get("group_id", "")
        if not group_id:
            return self._failure(code="INVALID_PARAMS", message="'group_id' is required.")
        result = await session.execute(
            select(GSageGroup)
            .where(
                GSageGroup.id == group_id,
                GSageGroup.org_id == agent_context.org_id,
            )
        )
        group = result.scalar_one_or_none()
        if not group:
            return self._failure(code="NOT_FOUND", message=f"Group '{group_id}' not found.")

        # Load permissions with dept_id via direct SQL join
        perm_result = await session.execute(
            select(
                GSagePermission.tag,
                gsage_group_permissions.c.dept_id,
            )
            .join(
                gsage_group_permissions,
                GSagePermission.id == gsage_group_permissions.c.permission_id,
            )
            .where(gsage_group_permissions.c.group_id == group.id)
            .order_by(GSagePermission.tag)
        )
        dept_perms = [
            {"tag": row.tag, "dept_id": str(row.dept_id) if row.dept_id else None}
            for row in perm_result.all()
        ]

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(group, dept_perms), execution_time_ms=elapsed)

    async def _handle_create(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        name = params.get("name", "").strip()
        if not name:
            return self._failure(code="INVALID_PARAMS", message="'name' is required.")
        group = GSageGroup(
            org_id=agent_context.org_id,
            name=name,
            description=params.get("description"),
        )
        session.add(group)
        await session.commit()
        await session.refresh(group)
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(group), execution_time_ms=elapsed)

    async def _handle_update(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        group_id = params.get("group_id", "")
        if not group_id:
            return self._failure(code="INVALID_PARAMS", message="'group_id' is required.")
        result = await session.execute(
            select(GSageGroup).where(
                GSageGroup.id == group_id,
                GSageGroup.org_id == agent_context.org_id,
            )
        )
        group = result.scalar_one_or_none()
        if not group:
            return self._failure(code="NOT_FOUND", message=f"Group '{group_id}' not found.")
        if "name" in params:
            group.name = params["name"]
        if "description" in params:
            group.description = params["description"]
        await session.commit()
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(data=_serialize(group), execution_time_ms=elapsed)

    async def _handle_delete(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        group_id = params.get("group_id", "")
        if not group_id:
            return self._failure(code="INVALID_PARAMS", message="'group_id' is required.")

        user_group_ids = await self._load_user_group_ids(agent_context, session)
        if group_id in user_group_ids:
            return self._failure(
                code="FORBIDDEN",
                message="Cannot delete a group you currently belong to.",
            )

        result = await session.execute(
            select(GSageGroup).where(
                GSageGroup.id == group_id,
                GSageGroup.org_id == agent_context.org_id,
            )
        )
        group = result.scalar_one_or_none()
        if not group:
            return self._failure(code="NOT_FOUND", message=f"Group '{group_id}' not found.")

        await session.delete(group)
        await session.commit()
        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"id": group_id, "deleted": True, "name": group.name},
            execution_time_ms=elapsed,
        )

    async def _handle_add_permission(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        group_id = params.get("group_id", "")
        tag = params.get("permission_tag", "").strip()
        raw_dept_id: str | None = params.get("dept_id")
        if not group_id or not tag:
            return self._failure(code="INVALID_PARAMS", message="'group_id' and 'permission_tag' are required.")

        dept_id: uuid.UUID | None = None
        if raw_dept_id:
            try:
                dept_id = uuid.UUID(raw_dept_id)
            except ValueError:
                return self._failure(code="INVALID_PARAMS", message=f"'dept_id' is not a valid UUID: {raw_dept_id}")

        group_result = await session.execute(
            select(GSageGroup)
            .where(GSageGroup.id == group_id, GSageGroup.org_id == agent_context.org_id)
        )
        group = group_result.scalar_one_or_none()
        if not group:
            return self._failure(code="NOT_FOUND", message=f"Group '{group_id}' not found.")

        perm_result = await session.execute(
            select(GSagePermission).where(GSagePermission.tag == tag)
        )
        perm = perm_result.scalar_one_or_none()
        if not perm:
            return self._failure(code="NOT_FOUND", message=f"Permission tag '{tag}' not found.")

        # Check for duplicate (same group + permission + dept_id scope)
        existing = await session.execute(
            select(gsage_group_permissions.c.group_id)
            .where(
                gsage_group_permissions.c.group_id == group.id,
                gsage_group_permissions.c.permission_id == perm.id,
                (
                    gsage_group_permissions.c.dept_id.is_(None)
                    if dept_id is None
                    else gsage_group_permissions.c.dept_id == dept_id
                ),
            )
        )
        if existing.first() is None:
            await session.execute(
                insert(gsage_group_permissions).values(
                    {"group_id": group.id, "permission_id": perm.id, "dept_id": dept_id}
                )
            )
            await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"group_id": group_id, "permission_tag": tag, "dept_id": str(dept_id) if dept_id else None, "assigned": True},
            execution_time_ms=elapsed,
        )

    async def _handle_remove_permission(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        group_id = params.get("group_id", "")
        tag = params.get("permission_tag", "").strip()
        raw_dept_id: str | None = params.get("dept_id")
        if not group_id or not tag:
            return self._failure(code="INVALID_PARAMS", message="'group_id' and 'permission_tag' are required.")

        dept_id: uuid.UUID | None = None
        if raw_dept_id:
            try:
                dept_id = uuid.UUID(raw_dept_id)
            except ValueError:
                return self._failure(code="INVALID_PARAMS", message=f"'dept_id' is not a valid UUID: {raw_dept_id}")

        group_result = await session.execute(
            select(GSageGroup)
            .where(GSageGroup.id == group_id, GSageGroup.org_id == agent_context.org_id)
        )
        group = group_result.scalar_one_or_none()
        if not group:
            return self._failure(code="NOT_FOUND", message=f"Group '{group_id}' not found.")

        perm_result = await session.execute(
            select(GSagePermission).where(GSagePermission.tag == tag)
        )
        perm = perm_result.scalar_one_or_none()
        if not perm:
            return self._failure(code="NOT_FOUND", message=f"Permission tag '{tag}' not found.")

        # Delete scope-specific or all rows for this (group, permission) pair
        if dept_id is not None:
            # Remove only the specific dept-scoped row
            await session.execute(
                delete(gsage_group_permissions).where(
                    gsage_group_permissions.c.group_id == group.id,
                    gsage_group_permissions.c.permission_id == perm.id,
                    gsage_group_permissions.c.dept_id == dept_id,
                )
            )
        else:
            # Remove ALL rows for this (group, permission) pair (global + all dept-scoped)
            await session.execute(
                delete(gsage_group_permissions).where(
                    gsage_group_permissions.c.group_id == group.id,
                    gsage_group_permissions.c.permission_id == perm.id,
                )
            )
        await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"group_id": group_id, "permission_tag": tag, "dept_id": str(dept_id) if dept_id else None, "removed": True},
            execution_time_ms=elapsed,
        )
