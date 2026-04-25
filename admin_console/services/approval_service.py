"""Admin Console — service functions for Approval Rules."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def list_approval_rules(
    db: AsyncSession,
) -> list[dict[str, Any]]:
    from src.shared.models.approval_rule import GSageApprovalRule  # noqa: PLC0415

    result = await db.execute(
        select(GSageApprovalRule).order_by(
            GSageApprovalRule.priority, GSageApprovalRule.tool_pattern
        )
    )
    return [_rule_to_dict(r) for r in result.scalars().all()]


async def create_approval_rule(
    db: AsyncSession,
    org_id_pattern: str,
    user_id_pattern: str,
    dept_id_pattern: str,
    tool_pattern: str,
    approver_user_id: uuid.UUID,
    priority: int = 100,
    description: str = "",
) -> dict[str, Any]:
    from src.shared.models.approval_rule import GSageApprovalRule  # noqa: PLC0415

    rule = GSageApprovalRule(
        org_id_pattern=org_id_pattern.strip(),
        user_id_pattern=user_id_pattern.strip(),
        dept_id_pattern=dept_id_pattern.strip() or "*",
        tool_pattern=tool_pattern.strip(),
        approver_user_id=approver_user_id,
        priority=priority,
        description=description.strip() or None,
        is_active=True,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return _rule_to_dict(rule)


async def update_approval_rule(
    db: AsyncSession,
    rule_id: uuid.UUID,
    **fields: Any,
) -> Optional[dict[str, Any]]:
    from src.shared.models.approval_rule import GSageApprovalRule  # noqa: PLC0415

    await db.execute(
        update(GSageApprovalRule)
        .where(GSageApprovalRule.id == rule_id)
        .values(**fields)
    )
    await db.commit()
    result = await db.execute(
        select(GSageApprovalRule).where(GSageApprovalRule.id == rule_id)
    )
    r = result.scalar_one_or_none()
    return _rule_to_dict(r) if r else None


async def delete_approval_rule(db: AsyncSession, rule_id: uuid.UUID) -> bool:
    from src.shared.models.approval_rule import GSageApprovalRule  # noqa: PLC0415

    await db.execute(
        delete(GSageApprovalRule).where(GSageApprovalRule.id == rule_id)
    )
    await db.commit()
    return True


def _rule_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "org_id_pattern": r.org_id_pattern,
        "dept_id_pattern": r.dept_id_pattern,
        "user_id_pattern": r.user_id_pattern,
        "tool_pattern": r.tool_pattern,
        "approver_user_id": str(r.approver_user_id),
        "priority": r.priority,
        "is_active": r.is_active,
        "description": r.description or "",
    }
