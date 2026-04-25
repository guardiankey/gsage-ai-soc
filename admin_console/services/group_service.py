"""Admin Console — service functions for Groups and Permissions."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


async def list_groups(
    db: AsyncSession,
    org_id: uuid.UUID,
) -> list[dict[str, Any]]:
    from src.shared.models.group import GSageGroup  # noqa: PLC0415

    result = await db.execute(
        select(GSageGroup)
        .where(GSageGroup.org_id == org_id)
        .options(selectinload(GSageGroup.permissions))
        .order_by(GSageGroup.name)
    )
    return [_group_to_dict(g) for g in result.scalars().all()]


async def get_group(
    db: AsyncSession,
    group_id: uuid.UUID,
) -> Optional[dict[str, Any]]:
    from src.shared.models.group import GSageGroup  # noqa: PLC0415

    result = await db.execute(
        select(GSageGroup)
        .where(GSageGroup.id == group_id)
        .options(
            selectinload(GSageGroup.permissions),
            selectinload(GSageGroup.users),
        )
    )
    g = result.scalar_one_or_none()
    return _group_to_dict(g, include_users=True) if g else None


async def create_group(
    db: AsyncSession,
    org_id: uuid.UUID,
    name: str,
    description: str = "",
) -> dict[str, Any]:
    from src.shared.models.group import GSageGroup  # noqa: PLC0415

    group = GSageGroup(
        org_id=org_id,
        name=name.strip(),
        description=description.strip() or None,
    )
    db.add(group)
    await db.commit()
    # Re-fetch with eager-loaded permissions — db.refresh() expires lazy
    # relationships causing greenlet_spawn errors on subsequent access.
    fetched = await db.execute(
        select(GSageGroup)
        .where(GSageGroup.id == group.id)
        .options(selectinload(GSageGroup.permissions))
    )
    return _group_to_dict(fetched.scalar_one())


async def delete_group(db: AsyncSession, group_id: uuid.UUID) -> bool:
    from src.shared.models.group import GSageGroup  # noqa: PLC0415
    from sqlalchemy.orm import selectinload  # noqa: PLC0415

    # Load group with users to get IDs for cache invalidation
    result = await db.execute(
        select(GSageGroup)
        .where(GSageGroup.id == group_id)
        .options(selectinload(GSageGroup.users))
    )
    group = result.scalar_one_or_none()
    if group is None:
        return False

    org_id = group.org_id
    affected_user_ids = [u.id for u in group.users]

    await db.delete(group)
    await db.commit()

    if affected_user_ids:
        from src.shared.cache.permissions_cache import (  # noqa: PLC0415
            get_perm_redis_client,
            invalidate_user_permissions,
        )
        rc = get_perm_redis_client()
        if rc is not None:
            for uid in affected_user_ids:
                await invalidate_user_permissions(rc, org_id, uid)

    return True


async def list_all_permissions(db: AsyncSession) -> list[dict[str, Any]]:
    """Return all permissions globally."""
    from src.shared.models.permission import GSagePermission  # noqa: PLC0415

    result = await db.execute(
        select(GSagePermission).order_by(
            GSagePermission.category, GSagePermission.tag
        )
    )
    return [
        {"id": str(p.id), "tag": p.tag, "description": p.description, "category": p.category}
        for p in result.scalars().all()
    ]


async def set_group_permissions(
    db: AsyncSession,
    group_id: uuid.UUID,
    permission_ids: list[uuid.UUID],
    *,
    dept_id: Optional[uuid.UUID] = None,
) -> bool:
    """Replace permissions for a group, scoped by dept_id.

    If ``dept_id`` is None, replaces only global permissions (dept_id IS NULL).
    If ``dept_id`` is provided, replaces only that department-scoped permissions.
    Other scopes are not affected.
    """
    from src.shared.models.group import GSageGroup, gsage_group_permissions  # noqa: PLC0415

    # Verify group exists
    result = await db.execute(
        select(GSageGroup)
        .where(GSageGroup.id == group_id)
        .options(selectinload(GSageGroup.users))
    )
    group_obj = result.scalar_one_or_none()
    if group_obj is None:
        return False

    org_id = group_obj.org_id
    affected_user_ids = [u.id for u in group_obj.users]

    gpc = gsage_group_permissions.c
    scope_filter = gpc.dept_id.is_(None) if dept_id is None else gpc.dept_id == dept_id

    await db.execute(
        delete(gsage_group_permissions).where(
            gpc.group_id == group_id,
            scope_filter,
        )
    )

    if permission_ids:
        await db.execute(
            insert(gsage_group_permissions).values(
                [
                    {"group_id": group_id, "permission_id": pid, "dept_id": dept_id}
                    for pid in permission_ids
                ]
            )
        )

    await db.commit()

    if affected_user_ids:
        from src.shared.cache.permissions_cache import (  # noqa: PLC0415
            get_perm_redis_client,
            invalidate_user_permissions,
        )
        rc = get_perm_redis_client()
        if rc is not None:
            for uid in affected_user_ids:
                await invalidate_user_permissions(rc, org_id, uid)

    return True


async def set_group_users(
    db: AsyncSession,
    group_id: uuid.UUID,
    user_ids: list[uuid.UUID],
) -> bool:
    """Replace all users for a group."""
    from src.shared.models.group import GSageGroup  # noqa: PLC0415
    from src.shared.models.user import GSageUser  # noqa: PLC0415

    result = await db.execute(
        select(GSageGroup)
        .where(GSageGroup.id == group_id)
        .options(selectinload(GSageGroup.users))
    )
    group = result.scalar_one_or_none()
    if not group:
        return False

    org_id = group.org_id
    old_user_ids = {u.id for u in group.users}

    user_result = await db.execute(
        select(GSageUser).where(GSageUser.id.in_(user_ids))
    )
    group.users = list(user_result.scalars().all())
    await db.commit()

    affected = old_user_ids | set(user_ids)
    if affected:
        from src.shared.cache.permissions_cache import (  # noqa: PLC0415
            get_perm_redis_client,
            invalidate_user_permissions,
        )
        rc = get_perm_redis_client()
        if rc is not None:
            for uid in affected:
                await invalidate_user_permissions(rc, org_id, uid)

    return True


def _group_to_dict(group: Any, include_users: bool = False) -> dict[str, Any]:
    """Serialize a GSageGroup to dict.

    NOTE: ORM-loaded permissions via the relationship do not expose dept_id.
    Use ``get_group_permissions_with_dept`` for a dept-aware listing.
    """
    d: dict[str, Any] = {
        "id": str(group.id),
        "name": group.name,
        "description": group.description or "",
        "org_id": str(group.org_id),
        "permissions": [
            {"id": str(p.id), "tag": p.tag, "category": p.category, "dept_id": None}
            for p in (group.permissions or [])
        ],
        "permission_count": len(group.permissions or []),
    }
    if include_users:
        d["users"] = [
            {"id": str(u.id), "email": u.email}
            for u in (group.users or [])
        ]
    return d


async def get_group_permissions_with_dept(
    db: AsyncSession,
    group_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Return permissions for a group with their dept_id scope, via direct SQL join."""
    from src.shared.models.group import gsage_group_permissions  # noqa: PLC0415
    from src.shared.models.permission import GSagePermission  # noqa: PLC0415

    result = await db.execute(
        select(
            GSagePermission.id,
            GSagePermission.tag,
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
        {
            "id": str(row.id),
            "tag": row.tag,
            "category": row.category,
            "dept_id": str(row.dept_id) if row.dept_id else None,
        }
        for row in result.all()
    ]
