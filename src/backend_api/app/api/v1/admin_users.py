"""gSage AI — Admin: User management endpoints.

Routes (prefix: /v1/orgs/{org_id}/admin):
    GET    /users                         List users in org (paginated, searchable)
    POST   /users                         Create user and add to org
    GET    /users/{user_id}               User detail with groups and departments
    PATCH  /users/{user_id}               Update user profile or org role
    DELETE /users/{user_id}               Deactivate membership (does not delete user globally)
    POST   /users/{user_id}/reset-password  Reset password, returns temp password
    POST   /users/{user_id}/reset-otp       Disable OTP for user
    PUT    /users/{user_id}/groups          Replace user's group memberships
    PUT    /users/{user_id}/departments     Replace user's department memberships
"""

from __future__ import annotations

import secrets
import string
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.backend_api.app.api.deps import get_db, require_org_admin
from src.backend_api.app.schemas.admin import (
    AdminUserCreate,
    AdminUserDetail,
    AdminUserDepartmentsUpdate,
    AdminUserGroupsUpdate,
    AdminUserOut,
    AdminUserUpdate,
    GroupOut,
    ResetPasswordResponse,
)
from src.backend_api.app.schemas.pagination import PaginatedResponse, PaginationParams
from src.shared.models.department import GSageDepartment
from src.shared.models.group import GSageGroup
from src.shared.models.user import GSageUser, gsage_user_groups
from src.shared.models.user_department import GSageUserDepartment
from src.shared.models.user_organization import GSageUserOrganization
from src.shared.security.auth import hash_password

router = APIRouter()


def _temp_password(length: int = 16) -> str:
    """Generate a cryptographically secure temporary password."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def _build_user_out(
    db: AsyncSession,
    user: GSageUser,
    org_id: uuid.UUID,
) -> AdminUserOut:
    """Build AdminUserOut for a user, resolving their role in org."""
    membership = await db.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == user.id,
            GSageUserOrganization.org_id == org_id,
        )
    )
    mem = membership.scalar_one_or_none()
    role_in_org = mem.role if mem else "member"

    # Group IDs (org-scoped)
    groups_result = await db.execute(
        select(GSageGroup.id)
        .join(gsage_user_groups, GSageGroup.id == gsage_user_groups.c.group_id)
        .where(
            gsage_user_groups.c.user_id == user.id,
            GSageGroup.org_id == org_id,
        )
    )
    group_ids = [row[0] for row in groups_result.all()]

    # Dept IDs
    depts_result = await db.execute(
        select(GSageUserDepartment.dept_id)
        .join(GSageDepartment, GSageUserDepartment.dept_id == GSageDepartment.id)
        .where(
            GSageUserDepartment.user_id == user.id,
            GSageDepartment.org_id == org_id,
            GSageUserDepartment.is_active == True,  # noqa: E712
        )
    )
    dept_ids = [row[0] for row in depts_result.all()]

    return AdminUserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_active=user.is_active,
        auth_provider=user.auth_provider,
        otp_enabled=user.otp_enabled,
        role_in_org=role_in_org,
        group_ids=group_ids,
        telegram_id=user.telegram_id,
        ai_instructions=user.ai_instructions,
        secondary_emails=user.secondary_emails,
        dept_ids=dept_ids,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.get(
    "/users",
    response_model=PaginatedResponse[AdminUserOut],
    summary="List users in organization",
)
async def list_org_users(
    org_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
    pagination: PaginationParams = Depends(),
    search: Optional[str] = Query(default=None, description="Search by name or email"),
) -> PaginatedResponse[AdminUserOut]:
    """List all users who are members of the organization."""
    base_stmt = (
        select(GSageUser)
        .join(
            GSageUserOrganization,
            and_(
                GSageUserOrganization.user_id == GSageUser.id,
                GSageUserOrganization.org_id == org_id,
            ),
        )
    )
    if search:
        like = f"%{search}%"
        from sqlalchemy import or_
        base_stmt = base_stmt.where(
            or_(
                GSageUser.email.ilike(like),
                GSageUser.full_name.ilike(like),
            )
        )

    count_result = await db.execute(select(func.count()).select_from(base_stmt.subquery()))
    total = count_result.scalar_one()

    users_result = await db.execute(
        base_stmt.order_by(GSageUser.full_name).offset(pagination.offset).limit(pagination.limit)
    )
    users = users_result.scalars().all()

    items = [await _build_user_out(db, u, org_id) for u in users]
    return PaginatedResponse.build(items, total=total, pagination=pagination)


@router.post(
    "/users",
    response_model=AdminUserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create user and add to organization",
)
async def create_org_user(
    org_id: uuid.UUID,
    payload: AdminUserCreate,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> AdminUserOut:
    """Create a new user and add them to the organization with the given role."""
    # Check email uniqueness
    existing = await db.execute(
        select(GSageUser).where(GSageUser.email == payload.email)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = GSageUser(
        email=str(payload.email),
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        is_active=True,
        auth_provider="local",
    )
    db.add(user)
    await db.flush()  # get user.id

    membership = GSageUserOrganization(
        user_id=user.id,
        org_id=org_id,
        role=payload.role,
        is_active=True,
    )
    db.add(membership)
    await db.commit()
    await db.refresh(user)

    return await _build_user_out(db, user, org_id)


@router.get(
    "/users/{user_id}",
    response_model=AdminUserDetail,
    summary="Get user detail",
)
async def get_org_user(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> AdminUserDetail:
    """Get detailed info for a user including groups and departments."""
    membership_result = await db.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == user_id,
            GSageUserOrganization.org_id == org_id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found in organization")

    user_result = await db.execute(select(GSageUser).where(GSageUser.id == user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Groups
    groups_result = await db.execute(
        select(GSageGroup)
        .join(gsage_user_groups, GSageGroup.id == gsage_user_groups.c.group_id)
        .where(
            gsage_user_groups.c.user_id == user_id,
            GSageGroup.org_id == org_id,
        )
    )
    groups = groups_result.scalars().all()

    # Departments with role
    dept_memberships_result = await db.execute(
        select(GSageUserDepartment, GSageDepartment)
        .join(GSageDepartment, GSageUserDepartment.dept_id == GSageDepartment.id)
        .where(
            GSageUserDepartment.user_id == user_id,
            GSageDepartment.org_id == org_id,
            GSageUserDepartment.is_active == True,  # noqa: E712
        )
    )
    dept_rows = dept_memberships_result.all()

    base = await _build_user_out(db, user, org_id)

    return AdminUserDetail(
        **base.model_dump(),
        groups=[
            GroupOut(
                id=g.id,
                org_id=g.org_id,
                name=g.name,
                description=g.description,
                member_count=0,
                permission_tags=[],
                created_at=g.created_at,
                updated_at=g.updated_at,
            )
            for g in groups
        ],
        departments=[
            {"dept_id": str(ud.dept_id), "dept_name": dept.name, "role": ud.role}
            for ud, dept in dept_rows
        ],
    )


@router.patch(
    "/users/{user_id}",
    response_model=AdminUserOut,
    summary="Update user profile or org role",
)
async def update_org_user(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: AdminUserUpdate,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> AdminUserOut:
    """Update user profile fields and/or their role in the organization."""
    membership_result = await db.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == user_id,
            GSageUserOrganization.org_id == org_id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found in organization")

    user_result = await db.execute(select(GSageUser).where(GSageUser.id == user_id))
    user = user_result.scalar_one()

    update_data = payload.model_dump(exclude_unset=True)
    if "role" in update_data:
        membership.role = update_data.pop("role")
    for field, value in update_data.items():
        setattr(user, field, value)

    await db.commit()
    await db.refresh(user)
    return await _build_user_out(db, user, org_id)


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deactivate user membership",
)
async def deactivate_org_user(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> None:
    """Deactivate a user's membership in the organization (does not delete the global user)."""
    membership_result = await db.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == user_id,
            GSageUserOrganization.org_id == org_id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found in organization")
    membership.is_active = False
    await db.commit()


@router.post(
    "/users/{user_id}/reset-password",
    response_model=ResetPasswordResponse,
    summary="Reset user password",
)
async def reset_user_password(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> ResetPasswordResponse:
    """Generate a temporary password for the user. Admin must communicate it securely."""
    membership_result = await db.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == user_id,
            GSageUserOrganization.org_id == org_id,
        )
    )
    if membership_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found in organization")

    user_result = await db.execute(select(GSageUser).where(GSageUser.id == user_id))
    user = user_result.scalar_one()

    temp_pw = _temp_password()
    user.password_hash = hash_password(temp_pw)
    await db.commit()

    return ResetPasswordResponse(temporary_password=temp_pw)


@router.post(
    "/users/{user_id}/reset-otp",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disable OTP for user",
)
async def reset_user_otp(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> None:
    """Disable and clear OTP configuration for a user."""
    membership_result = await db.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == user_id,
            GSageUserOrganization.org_id == org_id,
        )
    )
    if membership_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found in organization")

    user_result = await db.execute(select(GSageUser).where(GSageUser.id == user_id))
    user = user_result.scalar_one()

    user._otp_secret_encrypted = None
    user.otp_enabled = False
    user.otp_confirmed_at = None
    user._otp_backup_codes_encrypted = None
    await db.commit()


@router.put(
    "/users/{user_id}/groups",
    response_model=AdminUserOut,
    summary="Replace user's group memberships",
)
async def update_user_groups(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: AdminUserGroupsUpdate,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> AdminUserOut:
    """Replace all group memberships for a user (org-scoped groups only)."""
    membership_result = await db.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == user_id,
            GSageUserOrganization.org_id == org_id,
        )
    )
    if membership_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found in organization")

    # Validate all group IDs belong to this org
    if payload.group_ids:
        valid_groups = await db.execute(
            select(GSageGroup.id).where(
                GSageGroup.id.in_(payload.group_ids),
                GSageGroup.org_id == org_id,
            )
        )
        valid_ids = {row[0] for row in valid_groups.all()}
        invalid = set(payload.group_ids) - valid_ids
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Group IDs not found in organization: {[str(i) for i in invalid]}",
            )

    # Remove all current org-group memberships
    org_groups_result = await db.execute(
        select(GSageGroup.id).where(GSageGroup.org_id == org_id)
    )
    org_group_ids = [row[0] for row in org_groups_result.all()]

    if org_group_ids:
        await db.execute(
            delete(gsage_user_groups).where(
                and_(
                    gsage_user_groups.c.user_id == user_id,
                    gsage_user_groups.c.group_id.in_(org_group_ids),
                )
            )
        )

    # Insert new memberships
    if payload.group_ids:
        await db.execute(
            insert(gsage_user_groups).values(
                [{"user_id": user_id, "group_id": gid} for gid in payload.group_ids]
            )
        )

    await db.commit()

    user_result = await db.execute(select(GSageUser).where(GSageUser.id == user_id))
    user = user_result.scalar_one()
    return await _build_user_out(db, user, org_id)


@router.put(
    "/users/{user_id}/departments",
    response_model=AdminUserOut,
    summary="Replace user's department memberships",
)
async def update_user_departments(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: AdminUserDepartmentsUpdate,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> AdminUserOut:
    """Replace all department memberships for a user (org-scoped departments only)."""
    membership_result = await db.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == user_id,
            GSageUserOrganization.org_id == org_id,
        )
    )
    if membership_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found in organization")

    # Validate dept IDs
    if payload.assignments:
        dept_ids = [a.dept_id for a in payload.assignments]
        valid_depts = await db.execute(
            select(GSageDepartment.id).where(
                GSageDepartment.id.in_(dept_ids),
                GSageDepartment.org_id == org_id,
            )
        )
        valid_ids = {row[0] for row in valid_depts.all()}
        invalid = set(dept_ids) - valid_ids
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Department IDs not found in organization: {[str(i) for i in invalid]}",
            )

    # Remove existing dept memberships for this org
    org_depts_result = await db.execute(
        select(GSageDepartment.id).where(GSageDepartment.org_id == org_id)
    )
    org_dept_ids = [row[0] for row in org_depts_result.all()]

    if org_dept_ids:
        await db.execute(
            delete(GSageUserDepartment).where(
                and_(
                    GSageUserDepartment.user_id == user_id,
                    GSageUserDepartment.dept_id.in_(org_dept_ids),
                )
            )
        )

    # Insert new dept memberships
    for assignment in payload.assignments:
        db.add(
            GSageUserDepartment(
                user_id=user_id,
                dept_id=assignment.dept_id,
                role=assignment.role,
                is_active=True,
            )
        )

    await db.commit()

    user_result = await db.execute(select(GSageUser).where(GSageUser.id == user_id))
    user = user_result.scalar_one()
    return await _build_user_out(db, user, org_id)
