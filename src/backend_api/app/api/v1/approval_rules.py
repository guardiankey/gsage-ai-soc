"""gSage AI — Approval Rules REST endpoints.

Routes
------
GET    /orgs/{org_id}/members                                  List org members (for approver dropdown)
GET    /orgs/{org_id}/approval-rules                           Paginated list (filters: is_active, tool_pattern)
GET    /orgs/{org_id}/approval-rules/{rule_id}                 Get single rule
POST   /orgs/{org_id}/approval-rules                           Create rule
PATCH  /orgs/{org_id}/approval-rules/{rule_id}                 Update rule
DELETE /orgs/{org_id}/approval-rules/{rule_id}                 Delete rule
POST   /orgs/{org_id}/approval-rules/{rule_id}/activate        Set is_active=True
POST   /orgs/{org_id}/approval-rules/{rule_id}/deactivate      Set is_active=False
"""

from __future__ import annotations

import uuid
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_tenant_context
from src.backend_api.app.core.tenant import TenantContext
from src.backend_api.app.schemas.approval_rule import (
    ApprovalRuleCreate,
    ApprovalRuleOut,
    ApprovalRuleUpdate,
)
from src.backend_api.app.schemas.pagination import PaginatedResponse, PaginationParams, paginate_query
from src.shared.database import get_db
from src.shared.models.approval_rule import GSageApprovalRule
from src.shared.models.user import GSageUser
from src.shared.models.user_department import GSageUserDepartment
from src.shared.models.user_organization import GSageUserOrganization

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class OrgMemberOut(BaseModel):
    user_id: uuid.UUID
    full_name: str
    email: str
    role: str


async def _validate_dept_pattern(
    dept_id_pattern: str,
    ctx: TenantContext,
    db: AsyncSession,
) -> None:
    """Raise 403/422 if the caller is not authorised to operate on this dept_id_pattern."""
    if dept_id_pattern == "*":
        if ctx.org_role not in ("owner", "admin"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only org admin/owner can create rules for all departments ('*').",
            )
        return
    # Validate it looks like a UUID
    try:
        target_dept_id = uuid.UUID(dept_id_pattern)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="dept_id_pattern must be a valid UUID or '*'.",
        )
    # Org admin/owner can target any department
    if ctx.org_role in ("owner", "admin"):
        return
    # Otherwise the caller must be an admin of that specific department
    result = await db.execute(
        select(GSageUserDepartment).where(
            GSageUserDepartment.user_id == ctx.user_id,
            GSageUserDepartment.dept_id == target_dept_id,
            GSageUserDepartment.is_active.is_(True),
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None or membership.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be an admin of the target department to create rules for it.",
        )


async def _get_rule_or_404(
    rule_id: uuid.UUID,
    org_id: uuid.UUID,
    db: AsyncSession,
) -> GSageApprovalRule:
    result = await db.execute(
        select(GSageApprovalRule).where(
            GSageApprovalRule.id == rule_id,
            GSageApprovalRule.org_id_pattern == str(org_id),
        )
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval rule not found")
    return rule


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/orgs/{org_id}/members",
    response_model=List[OrgMemberOut],
    summary="List org members (used for approver selection)",
)
async def list_org_members(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> List[OrgMemberOut]:
    ctx.require_permission("approval_rules:read")

    result = await db.execute(
        select(GSageUser, GSageUserOrganization.role)
        .join(GSageUserOrganization, GSageUser.id == GSageUserOrganization.user_id)
        .where(
            GSageUserOrganization.org_id == ctx.org_id,
            GSageUserOrganization.is_active.is_(True),
            GSageUser.is_active.is_(True),
        )
        .order_by(GSageUser.full_name)
    )
    rows = result.all()
    return [
        OrgMemberOut(
            user_id=user.id,
            full_name=user.full_name,
            email=user.email,
            role=role,
        )
        for user, role in rows
    ]


@router.get(
    "/orgs/{org_id}/approval-rules",
    response_model=PaginatedResponse[ApprovalRuleOut],
    summary="List approval rules for the org",
)
async def list_approval_rules(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends()],
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    tool_pattern: Optional[str] = Query(None, description="Filter by tool_pattern (exact match)"),
) -> PaginatedResponse[ApprovalRuleOut]:
    ctx.require_permission("approval_rules:read")

    stmt = select(GSageApprovalRule).where(
        GSageApprovalRule.org_id_pattern == str(ctx.org_id),
    )
    if is_active is not None:
        stmt = stmt.where(GSageApprovalRule.is_active == is_active)
    if tool_pattern is not None:
        stmt = stmt.where(GSageApprovalRule.tool_pattern == tool_pattern)
    stmt = stmt.order_by(
        GSageApprovalRule.priority.desc(),
        GSageApprovalRule.created_at.desc(),
    )

    items, total = await paginate_query(db, stmt, pagination)
    return PaginatedResponse.build(
        [ApprovalRuleOut.model_validate(r) for r in items],
        total=total,
        pagination=pagination,
    )


@router.get(
    "/orgs/{org_id}/approval-rules/{rule_id}",
    response_model=ApprovalRuleOut,
    summary="Get an approval rule by ID",
)
async def get_approval_rule(
    org_id: uuid.UUID,
    rule_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApprovalRuleOut:
    ctx.require_permission("approval_rules:read")
    rule = await _get_rule_or_404(rule_id, ctx.org_id, db)
    return ApprovalRuleOut.model_validate(rule)


@router.post(
    "/orgs/{org_id}/approval-rules",
    response_model=ApprovalRuleOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new approval rule",
)
async def create_approval_rule(
    org_id: uuid.UUID,
    payload: ApprovalRuleCreate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApprovalRuleOut:
    ctx.require_permission("approval_rules:write")
    await _validate_dept_pattern(payload.dept_id_pattern, ctx, db)

    rule = GSageApprovalRule(
        org_id_pattern=str(ctx.org_id),
        dept_id_pattern=payload.dept_id_pattern,
        user_id_pattern=payload.user_id_pattern,
        tool_pattern=payload.tool_pattern,
        approver_user_id=payload.approver_user_id,
        is_active=payload.is_active,
        priority=payload.priority,
        description=payload.description,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return ApprovalRuleOut.model_validate(rule)


@router.patch(
    "/orgs/{org_id}/approval-rules/{rule_id}",
    response_model=ApprovalRuleOut,
    summary="Update an approval rule",
)
async def update_approval_rule(
    org_id: uuid.UUID,
    rule_id: uuid.UUID,
    payload: ApprovalRuleUpdate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApprovalRuleOut:
    ctx.require_permission("approval_rules:write")
    rule = await _get_rule_or_404(rule_id, ctx.org_id, db)

    update_data = payload.model_dump(exclude_unset=True)
    if "dept_id_pattern" in update_data:
        await _validate_dept_pattern(update_data["dept_id_pattern"], ctx, db)
    for field, value in update_data.items():
        setattr(rule, field, value)

    await db.commit()
    await db.refresh(rule)
    return ApprovalRuleOut.model_validate(rule)


@router.delete(
    "/orgs/{org_id}/approval-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an approval rule",
)
async def delete_approval_rule(
    org_id: uuid.UUID,
    rule_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    ctx.require_permission("approval_rules:write")
    rule = await _get_rule_or_404(rule_id, ctx.org_id, db)
    await db.delete(rule)
    await db.commit()


@router.post(
    "/orgs/{org_id}/approval-rules/{rule_id}/activate",
    response_model=ApprovalRuleOut,
    summary="Activate an approval rule",
)
async def activate_approval_rule(
    org_id: uuid.UUID,
    rule_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApprovalRuleOut:
    ctx.require_permission("approval_rules:write")
    rule = await _get_rule_or_404(rule_id, ctx.org_id, db)
    rule.is_active = True
    await db.commit()
    await db.refresh(rule)
    return ApprovalRuleOut.model_validate(rule)


@router.post(
    "/orgs/{org_id}/approval-rules/{rule_id}/deactivate",
    response_model=ApprovalRuleOut,
    summary="Deactivate an approval rule",
)
async def deactivate_approval_rule(
    org_id: uuid.UUID,
    rule_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApprovalRuleOut:
    ctx.require_permission("approval_rules:write")
    rule = await _get_rule_or_404(rule_id, ctx.org_id, db)
    rule.is_active = False
    await db.commit()
    await db.refresh(rule)
    return ApprovalRuleOut.model_validate(rule)
