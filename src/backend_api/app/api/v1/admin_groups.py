"""gSage AI — Admin: Group management endpoints.

Routes (prefix: /v1/orgs/{org_id}/admin):
    GET    /permissions              List all available permissions (for picker UI)
    GET    /groups                   List groups with member counts and permission tags
    POST   /groups                   Create a group
    GET    /groups/{group_id}        Group detail with members and permissions
    PATCH  /groups/{group_id}        Update group name/description
    DELETE /groups/{group_id}        Delete group
    PUT    /groups/{group_id}/members       Replace member list
    PUT    /groups/{group_id}/permissions   Replace permission list
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, delete, func, insert, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_db, require_org_admin
from src.backend_api.app.schemas.admin import (
    GroupCreate,
    GroupDetail,
    GroupMemberOut,
    GroupMembersUpdate,
    GroupOut,
    GroupPermissionEntry,
    GroupPermissionOut,
    GroupPermissionsUpdate,
    GroupUpdate,
    PermissionOut,
)
from src.shared.cache.permissions_cache import (
    get_perm_redis_client,
    invalidate_user_permissions,
)
from src.shared.models.group import GSageGroup, gsage_group_permissions
from src.shared.models.permission import GSagePermission
from src.shared.models.user import GSageUser, gsage_user_groups
from src.shared.models.user_organization import GSageUserOrganization

router = APIRouter()


async def _load_group_permissions(
    db: AsyncSession, group_id: uuid.UUID
) -> list[GroupPermissionOut]:
    """Load all permission assignments for a group, including dept_id info."""
    result = await db.execute(
        select(
            GSagePermission.id,
            GSagePermission.tag,
            GSagePermission.description,
            GSagePermission.category,
            gsage_group_permissions.c.dept_id,
        )
        .join(
            gsage_group_permissions,
            GSagePermission.id == gsage_group_permissions.c.permission_id,
        )
        .where(gsage_group_permissions.c.group_id == group_id)
        .order_by(GSagePermission.category, GSagePermission.tag)
    )
    return [
        GroupPermissionOut(
            id=row.id,
            tag=row.tag,
            description=row.description,
            category=row.category,
            dept_id=row.dept_id,
        )
        for row in result.all()
    ]


# ---------------------------------------------------------------------------
# Permissions (global list — needed for the dual-list picker in the UI)
# ---------------------------------------------------------------------------

@router.get(
    "/permissions",
    response_model=list[PermissionOut],
    summary="List all available permissions",
)
async def list_permissions(
    org_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> list[PermissionOut]:
    """Return all defined permissions, ordered by category then tag."""
    result = await db.execute(
        select(GSagePermission).order_by(GSagePermission.category, GSagePermission.tag)
    )
    return [PermissionOut.model_validate(p) for p in result.scalars().all()]


# ---------------------------------------------------------------------------
# Group CRUD
# ---------------------------------------------------------------------------

@router.get(
    "/groups",
    response_model=list[GroupOut],
    summary="List groups in organization",
)
async def list_groups(
    org_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> list[GroupOut]:
    """List all groups with member counts and permission tags."""
    result = await db.execute(
        select(GSageGroup)
        .options(selectinload(GSageGroup.users), selectinload(GSageGroup.permissions))
        .where(GSageGroup.org_id == org_id)
        .order_by(GSageGroup.name)
    )
    groups = result.scalars().all()

    return [
        GroupOut(
            id=g.id,
            org_id=g.org_id,
            name=g.name,
            description=g.description,
            member_count=len(g.users),
            permission_tags=[p.tag for p in g.permissions],
            created_at=g.created_at,
            updated_at=g.updated_at,
        )
        for g in groups
    ]


@router.post(
    "/groups",
    response_model=GroupOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a group",
)
async def create_group(
    org_id: uuid.UUID,
    payload: GroupCreate,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> GroupOut:
    """Create a new group in the organization."""
    clash = await db.execute(
        select(GSageGroup).where(
            GSageGroup.org_id == org_id,
            GSageGroup.name == payload.name,
        )
    )
    if clash.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Group name already exists in organization")

    group = GSageGroup(org_id=org_id, name=payload.name, description=payload.description)
    db.add(group)
    await db.commit()
    await db.refresh(group)

    return GroupOut(
        id=group.id,
        org_id=group.org_id,
        name=group.name,
        description=group.description,
        member_count=0,
        permission_tags=[],
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


@router.get(
    "/groups/{group_id}",
    response_model=GroupDetail,
    summary="Get group detail",
)
async def get_group(
    org_id: uuid.UUID,
    group_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> GroupDetail:
    """Get group detail including members and permissions."""
    result = await db.execute(
        select(GSageGroup)
        .options(
            selectinload(GSageGroup.users),
            selectinload(GSageGroup.permissions),
        )
        .where(GSageGroup.id == group_id, GSageGroup.org_id == org_id)
    )
    group = result.scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")

    return GroupDetail(
        id=group.id,
        org_id=group.org_id,
        name=group.name,
        description=group.description,
        member_count=len(group.users),
        permission_tags=[p.tag for p in group.permissions],
        created_at=group.created_at,
        updated_at=group.updated_at,
        members=[
            GroupMemberOut(user_id=u.id, email=u.email, full_name=u.full_name)
            for u in group.users
        ],
        permissions=await _load_group_permissions(db, group.id),
    )


@router.patch(
    "/groups/{group_id}",
    response_model=GroupOut,
    summary="Update group",
)
async def update_group(
    org_id: uuid.UUID,
    group_id: uuid.UUID,
    payload: GroupUpdate,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> GroupOut:
    """Update group name and/or description."""
    result = await db.execute(
        select(GSageGroup)
        .options(selectinload(GSageGroup.users), selectinload(GSageGroup.permissions))
        .where(GSageGroup.id == group_id, GSageGroup.org_id == org_id)
    )
    group = result.scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")

    if payload.name is not None and payload.name != group.name:
        clash = await db.execute(
            select(GSageGroup).where(
                GSageGroup.org_id == org_id,
                GSageGroup.name == payload.name,
                GSageGroup.id != group_id,
            )
        )
        if clash.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Group name already exists")
        group.name = payload.name

    if payload.description is not None:
        group.description = payload.description

    await db.commit()
    await db.refresh(group)

    return GroupOut(
        id=group.id,
        org_id=group.org_id,
        name=group.name,
        description=group.description,
        member_count=len(group.users),
        permission_tags=[p.tag for p in group.permissions],
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


@router.delete(
    "/groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete group",
)
async def delete_group(
    org_id: uuid.UUID,
    group_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a group. Cascades to user and permission associations."""
    result = await db.execute(
        select(GSageGroup)
        .options(selectinload(GSageGroup.users))
        .where(GSageGroup.id == group_id, GSageGroup.org_id == org_id)
    )
    group = result.scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")

    affected_user_ids = [u.id for u in group.users]

    await db.delete(group)
    await db.commit()

    # Invalidate permission cache for all ex-members
    if affected_user_ids:
        rc = get_perm_redis_client()
        if rc is not None:
            for uid in affected_user_ids:
                await invalidate_user_permissions(rc, org_id, uid)


# ---------------------------------------------------------------------------
# Group members
# ---------------------------------------------------------------------------

@router.put(
    "/groups/{group_id}/members",
    response_model=GroupDetail,
    summary="Replace group member list",
)
async def update_group_members(
    org_id: uuid.UUID,
    group_id: uuid.UUID,
    payload: GroupMembersUpdate,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> GroupDetail:
    """Replace all members of the group. Only org members can be added."""
    result = await db.execute(
        select(GSageGroup)
        .options(selectinload(GSageGroup.users), selectinload(GSageGroup.permissions))
        .where(GSageGroup.id == group_id, GSageGroup.org_id == org_id)
    )
    group = result.scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")

    # Capture old members for cache invalidation
    old_user_ids = {u.id for u in group.users}

    # Validate users are members of this org
    if payload.user_ids:
        from src.shared.models.user_organization import GSageUserOrganization as UO
        valid_users = await db.execute(
            select(UO.user_id).where(
                UO.user_id.in_(payload.user_ids),
                UO.org_id == org_id,
            )
        )
        valid_ids = {row[0] for row in valid_users.all()}
        invalid = set(payload.user_ids) - valid_ids
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"User IDs not found in organization: {[str(i) for i in invalid]}",
            )

    await db.execute(
        delete(gsage_user_groups).where(gsage_user_groups.c.group_id == group_id)
    )

    if payload.user_ids:
        await db.execute(
            insert(gsage_user_groups).values(
                [{"user_id": uid, "group_id": group_id} for uid in payload.user_ids]
            )
        )

    await db.commit()

    # Invalidate permission cache for all affected users (old + new)
    affected_user_ids = old_user_ids | set(payload.user_ids or [])
    if affected_user_ids:
        rc = get_perm_redis_client()
        if rc is not None:
            for uid in affected_user_ids:
                await invalidate_user_permissions(rc, org_id, uid)

    # Reload
    result = await db.execute(
        select(GSageGroup)
        .options(selectinload(GSageGroup.users), selectinload(GSageGroup.permissions))
        .where(GSageGroup.id == group_id)
    )
    group = result.scalar_one()

    return GroupDetail(
        id=group.id,
        org_id=group.org_id,
        name=group.name,
        description=group.description,
        member_count=len(group.users),
        permission_tags=[p.tag for p in group.permissions],
        created_at=group.created_at,
        updated_at=group.updated_at,
        members=[
            GroupMemberOut(user_id=u.id, email=u.email, full_name=u.full_name)
            for u in group.users
        ],
        permissions=await _load_group_permissions(db, group.id),
    )


# ---------------------------------------------------------------------------
# Group permissions
# ---------------------------------------------------------------------------

@router.put(
    "/groups/{group_id}/permissions",
    response_model=GroupDetail,
    summary="Replace group permission list",
)
async def update_group_permissions(
    org_id: uuid.UUID,
    group_id: uuid.UUID,
    payload: GroupPermissionsUpdate,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> GroupDetail:
    """Replace all permissions assigned to the group.

    Each entry specifies a ``permission_id`` and an optional ``dept_id``.
    ``dept_id=null`` means the permission is global (all departments);
    a non-null value restricts it to that specific department.
    """
    result = await db.execute(
        select(GSageGroup)
        .options(selectinload(GSageGroup.users), selectinload(GSageGroup.permissions))
        .where(GSageGroup.id == group_id, GSageGroup.org_id == org_id)
    )
    group = result.scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")

    # Capture members for cache invalidation after commit
    affected_user_ids = [u.id for u in group.users]

    # Validate permission IDs
    all_perm_ids = {e.permission_id for e in payload.permissions}
    if all_perm_ids:
        valid_perms = await db.execute(
            select(GSagePermission.id).where(
                GSagePermission.id.in_(all_perm_ids)
            )
        )
        valid_ids = {row[0] for row in valid_perms.all()}
        invalid = all_perm_ids - valid_ids
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Permission IDs not found: {[str(i) for i in invalid]}",
            )

    # Full replace: delete all existing assignments for this group
    await db.execute(
        delete(gsage_group_permissions).where(
            gsage_group_permissions.c.group_id == group_id
        )
    )

    if payload.permissions:
        await db.execute(
            insert(gsage_group_permissions).values(
                [
                    {
                        "group_id": group_id,
                        "permission_id": entry.permission_id,
                        "dept_id": entry.dept_id,
                    }
                    for entry in payload.permissions
                ]
            )
        )

    await db.commit()

    # Invalidate permission cache for all group members
    if affected_user_ids:
        rc = get_perm_redis_client()
        if rc is not None:
            for uid in affected_user_ids:
                await invalidate_user_permissions(rc, org_id, uid)

    # Reload
    result = await db.execute(
        select(GSageGroup)
        .options(selectinload(GSageGroup.users), selectinload(GSageGroup.permissions))
        .where(GSageGroup.id == group_id)
    )
    group = result.scalar_one()

    return GroupDetail(
        id=group.id,
        org_id=group.org_id,
        name=group.name,
        description=group.description,
        member_count=len(group.users),
        permission_tags=[p.tag for p in group.permissions],
        created_at=group.created_at,
        updated_at=group.updated_at,
        members=[
            GroupMemberOut(user_id=u.id, email=u.email, full_name=u.full_name)
            for u in group.users
        ],
        permissions=await _load_group_permissions(db, group.id),
    )
