"""gSage AI — Approval Rule CRUD tool.

Allows a department admin to manage GSageApprovalRule records **scoped to the
currently active department** (from ``X-Department-Id`` header).

    list    — list active approval rules for the current department  (crud:approval_rule:read)
    add     — create (or update) a rule for this department          (crud:approval_rule:write + dept admin)
    delete  — remove a rule by id (must belong to this department)   (crud:approval_rule:write + dept admin)

Notes:
    - A department context MUST be active (X-Department-Id header sent).
    - org_id_pattern is always set to the current org's UUID.
    - dept_id_pattern is always set to the current department's UUID (never "*").
    - user_id_pattern defaults to "*" (all users in dept) but can be a specific user UUID.
    - tool_pattern defaults to "*" (all tools) but can be an exact tool name.
    - Approver is resolved by name or email from active members of the current org.
    - Write operations require the caller to be a dept admin in the active department.
    - When a duplicate pattern (org+dept+user+tool) already exists the rule is updated in-place.
"""

from __future__ import annotations

import time
import uuid
from typing import ClassVar

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.mcp_server.tools.base import ToolResult
from src.mcp_server.tools.crud_base import CrudBaseTool
from src.shared.models.approval_rule import GSageApprovalRule
from src.shared.models.user import GSageUser
from src.shared.models.user_department import DepartmentRole, GSageUserDepartment
from src.shared.models.user_organization import GSageUserOrganization
from src.shared.security.context import AgentContext

_PERM_READ = "crud:approval_rule:read"
_PERM_WRITE = "crud:approval_rule:write"


def _serialize(rule: GSageApprovalRule, approver_name: str | None = None) -> dict:
    return {
        "id": str(rule.id),
        "org_id_pattern": rule.org_id_pattern,
        "dept_id_pattern": rule.dept_id_pattern,
        "user_id_pattern": rule.user_id_pattern,
        "tool_pattern": rule.tool_pattern,
        "approver_user_id": str(rule.approver_user_id),
        "approver_name": approver_name,
        "is_active": rule.is_active,
        "priority": rule.priority,
        "description": rule.description,
        "created_at": rule.created_at.isoformat(),
    }


async def _check_dept_admin(
    session: AsyncSession,
    agent_context: AgentContext,
) -> bool:
    """Return True if the user has the 'admin' role in the active department."""
    if agent_context.dept_id is None:
        return False
    result = await session.execute(
        select(GSageUserDepartment).where(
            GSageUserDepartment.user_id == agent_context.user_id,
            GSageUserDepartment.dept_id == agent_context.dept_id,
            GSageUserDepartment.role == DepartmentRole.ADMIN,
            GSageUserDepartment.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none() is not None


async def _resolve_approver(
    session: AsyncSession,
    approver: str,
    org_id: uuid.UUID,
) -> GSageUser | None:
    """Find an active org member by email (exact) or full_name (partial, case-insensitive).

    Returns the user only when a single match is found, or when exactly one of
    multiple candidates has a matching email address.
    """
    approver_clean = approver.strip()
    result = await session.execute(
        select(GSageUser)
        .join(GSageUserOrganization, GSageUserOrganization.user_id == GSageUser.id)
        .where(
            GSageUserOrganization.org_id == org_id,
            GSageUserOrganization.is_active.is_(True),
            or_(
                GSageUser.email.ilike(approver_clean),
                GSageUser.full_name.ilike(f"%{approver_clean}%"),
            ),
        )
        .limit(5)
    )
    users = result.scalars().all()

    if len(users) == 1:
        return users[0]
    if len(users) > 1:
        # Prefer exact email match to disambiguate
        for u in users:
            if u.email.lower() == approver_clean.lower():
                return u
    return None


class ApprovalRuleCrudTool(CrudBaseTool):
    """CRUD tool for GSageApprovalRule (per-org approval delegation rules)."""

    name: ClassVar[str] = "approval_rule"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = "Configure human-in-the-loop approval workflow rules for the active department (dept admin only)"
    category: ClassVar[str] = "crud"
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 15

    valid_actions: ClassVar[frozenset[str]] = frozenset({"list", "add", "delete"})
    write_actions: ClassVar[frozenset[str]] = frozenset({"add", "delete"})
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
                "enum": ["list", "add", "delete"],
                "description": (
                    "list: show all active approval rules for the current org. "
                    "add: create (or update) a rule — who must approve which tool calls. "
                    "delete: remove a rule by its UUID."
                ),
            },
            "approver": {
                "type": "string",
                "description": (
                    "[add] Name or email of the org member who will approve matching calls. "
                    "Resolved by exact email or partial full_name match among active org members. "
                    "Use a full email address when names are ambiguous."
                ),
            },
            "tool_pattern": {
                "type": "string",
                "description": (
                    "[add] Exact tool name to match, or '*' for all tools. "
                    "Examples: 'block_ip', 'dns_lookup', '*'. Defaults to '*'."
                ),
            },
            "user_id_pattern": {
                "type": "string",
                "description": (
                    "[add] UUID of the user whose calls must be approved, "
                    "or '*' to match calls from any user in the org. Defaults to '*'."
                ),
            },
            "priority": {
                "type": "integer",
                "description": (
                    "[add] Tie-break priority when multiple rules match with the same "
                    "specificity score. Higher value wins. Defaults to 0."
                ),
            },
            "description": {
                "type": "string",
                "description": "[add] Optional human-readable description of the rule.",
            },
            "rule_id": {
                "type": "string",
                "description": "[delete] UUID of the approval rule to remove.",
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
        if agent_context.dept_id is None:
            return self._failure(
                code="DEPT_REQUIRED",
                message="A department context is required. Please select an active department first.",
            )

        org_str = str(agent_context.org_id)
        dept_str = str(agent_context.dept_id)
        result = await session.execute(
            select(GSageApprovalRule)
            .where(
                GSageApprovalRule.org_id_pattern == org_str,
                GSageApprovalRule.dept_id_pattern == dept_str,
                GSageApprovalRule.is_active.is_(True),
            )
            .order_by(GSageApprovalRule.priority.desc(), GSageApprovalRule.created_at)
        )
        rules = result.scalars().all()

        # Batch-load approver display names
        approver_ids = list({r.approver_user_id for r in rules})
        approver_map: dict[uuid.UUID, str] = {}
        if approver_ids:
            user_result = await session.execute(
                select(GSageUser).where(GSageUser.id.in_(approver_ids))
            )
            for u in user_result.scalars().all():
                approver_map[u.id] = u.full_name or u.email

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={
                "rules": [_serialize(r, approver_map.get(r.approver_user_id)) for r in rules],
                "count": len(rules),
            },
            execution_time_ms=elapsed,
        )

    async def _handle_add(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        if agent_context.dept_id is None:
            return self._failure(
                code="DEPT_REQUIRED",
                message="A department context is required. Please select an active department first.",
            )

        if not await _check_dept_admin(session, agent_context):
            return self._failure(
                code="FORBIDDEN",
                message="Only department admins can create or modify approval rules.",
            )

        approver_str = (params.get("approver") or "").strip()
        if not approver_str:
            return self._failure(code="INVALID_PARAMS", message="'approver' is required for action 'add'.")

        tool_pattern = (params.get("tool_pattern") or "*").strip() or "*"
        user_id_pattern = (params.get("user_id_pattern") or "*").strip() or "*"
        priority = int(params.get("priority") or 0)
        description = params.get("description")
        org_str = str(agent_context.org_id)
        dept_str = str(agent_context.dept_id)

        # Resolve approver to an actual user
        approver_user = await _resolve_approver(session, approver_str, agent_context.org_id)
        if approver_user is None:
            return self._failure(
                code="NOT_FOUND",
                message=(
                    f"Could not uniquely resolve '{approver_str}' to an active org member. "
                    "Try using their full email address."
                ),
            )

        # Upsert: update existing rule with same pattern combination
        existing = await session.execute(
            select(GSageApprovalRule).where(
                GSageApprovalRule.org_id_pattern == org_str,
                GSageApprovalRule.dept_id_pattern == dept_str,
                GSageApprovalRule.user_id_pattern == user_id_pattern,
                GSageApprovalRule.tool_pattern == tool_pattern,
            )
        )
        rule = existing.scalar_one_or_none()

        if rule:
            rule.approver_user_id = approver_user.id
            rule.priority = priority
            rule.description = description
            rule.is_active = True
            created = False
        else:
            rule = GSageApprovalRule(
                org_id_pattern=org_str,
                dept_id_pattern=dept_str,
                user_id_pattern=user_id_pattern,
                tool_pattern=tool_pattern,
                approver_user_id=approver_user.id,
                priority=priority,
                description=description,
                is_active=True,
            )
            session.add(rule)
            created = True

        await session.commit()
        await session.refresh(rule)

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={
                **_serialize(rule, approver_user.full_name or approver_user.email),
                "created": created,
            },
            execution_time_ms=elapsed,
        )

    async def _handle_delete(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        session: AsyncSession,
        start: float,
    ) -> ToolResult:
        if agent_context.dept_id is None:
            return self._failure(
                code="DEPT_REQUIRED",
                message="A department context is required. Please select an active department first.",
            )

        if not await _check_dept_admin(session, agent_context):
            return self._failure(
                code="FORBIDDEN",
                message="Only department admins can delete approval rules.",
            )

        rule_id_str = (params.get("rule_id") or "").strip()
        if not rule_id_str:
            return self._failure(code="INVALID_PARAMS", message="'rule_id' is required for action 'delete'.")

        try:
            rule_id = uuid.UUID(rule_id_str)
        except ValueError:
            return self._failure(
                code="INVALID_PARAMS",
                message=f"'{rule_id_str}' is not a valid UUID.",
            )

        result = await session.execute(
            select(GSageApprovalRule).where(
                GSageApprovalRule.id == rule_id,
                GSageApprovalRule.org_id_pattern == str(agent_context.org_id),
                GSageApprovalRule.dept_id_pattern == str(agent_context.dept_id),
            )
        )
        rule = result.scalar_one_or_none()
        if not rule:
            return self._failure(
                code="NOT_FOUND",
                message=f"Rule '{rule_id_str}' not found in the active department.",
            )

        await session.delete(rule)
        await session.commit()

        elapsed = int((time.monotonic() - start) * 1000)
        return self._success(
            data={"deleted_rule_id": rule_id_str},
            execution_time_ms=elapsed,
        )
