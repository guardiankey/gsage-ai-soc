"""Admin Console — service functions for Departments."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def list_depts(db: AsyncSession, org_id: uuid.UUID) -> list[dict[str, Any]]:
    """Return all departments for an org ordered by name."""
    from src.shared.models.department import GSageDepartment  # noqa: PLC0415

    result = await db.execute(
        select(GSageDepartment)
        .where(GSageDepartment.org_id == org_id)
        .order_by(GSageDepartment.name)
    )
    rows = result.scalars().all()
    return [_dept_to_dict(d) for d in rows]


async def get_dept(db: AsyncSession, dept_id: uuid.UUID) -> Optional[dict[str, Any]]:
    """Return a single dept dict or None."""
    from src.shared.models.department import GSageDepartment  # noqa: PLC0415

    result = await db.execute(
        select(GSageDepartment).where(GSageDepartment.id == dept_id)
    )
    dept = result.scalar_one_or_none()
    return _dept_to_dict(dept) if dept else None


async def create_dept(
    db: AsyncSession,
    org_id: uuid.UUID,
    name: str,
    slug: str = "",
    is_active: bool = True,
) -> dict[str, Any]:
    """Create a new department for the given org."""
    from src.shared.models.department import GSageDepartment  # noqa: PLC0415

    effective_slug = slug.strip() if slug.strip() else name.lower().replace(" ", "-").replace("_", "-")
    dept = GSageDepartment(
        org_id=org_id,
        name=name.strip(),
        slug=effective_slug,
        is_default=False,
        is_active=is_active,
    )
    db.add(dept)
    await db.commit()
    await db.refresh(dept)
    return _dept_to_dict(dept)


async def update_dept(
    db: AsyncSession,
    dept_id: uuid.UUID,
    name: Optional[str] = None,
    slug: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Optional[dict[str, Any]]:
    """Update editable fields of a department."""
    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name.strip()
    if slug is not None:
        values["slug"] = slug.strip()
    if is_active is not None:
        values["is_active"] = is_active
    if values:
        from src.shared.models.department import GSageDepartment  # noqa: PLC0415

        await db.execute(
            update(GSageDepartment)
            .where(GSageDepartment.id == dept_id)
            .values(**values)
        )
        await db.commit()
    return await get_dept(db, dept_id)


async def delete_dept(db: AsyncSession, dept_id: uuid.UUID) -> bool:
    """Delete a department. Default departments cannot be deleted."""
    from src.shared.models.department import GSageDepartment  # noqa: PLC0415

    result = await db.execute(
        select(GSageDepartment).where(GSageDepartment.id == dept_id)
    )
    dept = result.scalar_one_or_none()
    if not dept:
        return False
    if dept.is_default:
        raise ValueError("Cannot delete the default department")
    await db.execute(
        delete(GSageDepartment).where(GSageDepartment.id == dept_id)
    )
    await db.commit()
    return True


async def toggle_dept_active(db: AsyncSession, dept_id: uuid.UUID) -> bool:
    """Toggle the is_active flag and return the new value."""
    dept_dict = await get_dept(db, dept_id)
    if not dept_dict:
        raise ValueError("Department not found")
    new_state = not dept_dict["is_active"]
    await update_dept(db, dept_id, is_active=new_state)
    return new_state


def _dept_to_dict(d: Any) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "name": d.name,
        "slug": d.slug,
        "is_default": d.is_default,
        "is_active": d.is_active,
        "org_id": str(d.org_id),
    }
