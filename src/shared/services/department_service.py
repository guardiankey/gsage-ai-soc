"""gSage AI — Department service layer.

Provides all business logic for GSageDepartment and GSageUserDepartment.
Functions receive an AsyncSession and operate within the caller's transaction.

Lifecycle notes
---------------
- Every org gets a "Default" department (is_default=True) at org creation.
  This is enforced by create_default_department(), called from org creation logic.
- The default department cannot be deleted (DepartmentDeleteError raised).
- Org admins always have implicit access to all departments in their org.
- dept_id is passed via X-Department-Id header (not in JWT).
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Optional, Sequence

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from src.shared.models.department import GSageDepartment
from src.shared.models.user_department import DepartmentRole, GSageUserDepartment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DepartmentError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class DepartmentNotFound(DepartmentError):
    def __init__(self, detail: str = "Department not found") -> None:
        super().__init__(detail, status_code=404)


class DepartmentConflict(DepartmentError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail, status_code=409)


class DepartmentDeleteError(DepartmentError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail, status_code=409)


class DepartmentAccessDenied(DepartmentError):
    def __init__(self, detail: str = "Access denied to department") -> None:
        super().__init__(detail, status_code=403)


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Convert a department name into a safe slug."""
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:100] or "dept"


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


async def get_department(
    db: AsyncSession,
    dept_id: uuid.UUID,
    org_id: uuid.UUID,
) -> GSageDepartment:
    """Return a department by id, scoped to org_id.

    Raises
    ------
    DepartmentNotFound — if the department does not exist or belongs to a
                         different org.
    """
    result = await db.execute(
        select(GSageDepartment).where(
            and_(
                GSageDepartment.id == dept_id,
                GSageDepartment.org_id == org_id,
            )
        )
    )
    dept = result.scalar_one_or_none()
    if dept is None:
        raise DepartmentNotFound()
    return dept


async def list_departments(
    db: AsyncSession,
    org_id: uuid.UUID,
    include_inactive: bool = False,
) -> Sequence[GSageDepartment]:
    """Return all departments for an organization."""
    filters = [GSageDepartment.org_id == org_id]
    if not include_inactive:
        filters.append(GSageDepartment.is_active.is_(True))
    result = await db.execute(
        select(GSageDepartment)
        .where(and_(*filters))
        .order_by(GSageDepartment.is_default.desc(), GSageDepartment.name)
    )
    return result.scalars().all()


async def get_default_department(
    db: AsyncSession,
    org_id: uuid.UUID,
) -> Optional[GSageDepartment]:
    """Return the default department for an org, or None if not found."""
    result = await db.execute(
        select(GSageDepartment).where(
            and_(
                GSageDepartment.org_id == org_id,
                GSageDepartment.is_default.is_(True),
            )
        )
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


async def create_department(
    db: AsyncSession,
    org_id: uuid.UUID,
    name: str,
    slug: Optional[str] = None,
    description: Optional[str] = None,
    is_default: bool = False,
) -> GSageDepartment:
    """Create a new department within an org.

    Raises
    ------
    DepartmentConflict — if name or slug is already taken in this org.
    """
    resolved_slug = slug or _slugify(name)

    # Enforce uniqueness manually for clearer error messages.
    exists = await db.execute(
        select(GSageDepartment.id).where(
            and_(
                GSageDepartment.org_id == org_id,
                GSageDepartment.slug == resolved_slug,
            )
        )
    )
    if exists.scalar_one_or_none():
        raise DepartmentConflict(f"A department with slug '{resolved_slug}' already exists in this org.")

    exists_name = await db.execute(
        select(GSageDepartment.id).where(
            and_(
                GSageDepartment.org_id == org_id,
                GSageDepartment.name == name,
            )
        )
    )
    if exists_name.scalar_one_or_none():
        raise DepartmentConflict(f"A department named '{name}' already exists in this org.")

    dept = GSageDepartment(
        org_id=org_id,
        name=name,
        slug=resolved_slug,
        description=description,
        is_default=is_default,
        is_active=True,
    )
    db.add(dept)
    await db.flush()
    await db.refresh(dept)
    logger.info("Created department %s (org=%s, default=%s)", dept.id, org_id, is_default)
    return dept


async def create_default_department(
    db: AsyncSession,
    org_id: uuid.UUID,
) -> GSageDepartment:
    """Create the 'Default' department for a newly created org."""
    return await create_department(
        db=db,
        org_id=org_id,
        name="Default",
        slug="default",
        description="Default department (auto-created for this organization).",
        is_default=True,
    )


async def update_department(
    db: AsyncSession,
    dept_id: uuid.UUID,
    org_id: uuid.UUID,
    name: Optional[str] = None,
    slug: Optional[str] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> GSageDepartment:
    """Update a department in-place.

    Raises
    ------
    DepartmentNotFound         — department doesn't exist.
    DepartmentConflict         — new name/slug conflicts with existing one.
    """
    dept = await get_department(db, dept_id=dept_id, org_id=org_id)

    if name is not None and name != dept.name:
        exists = await db.execute(
            select(GSageDepartment.id).where(
                and_(
                    GSageDepartment.org_id == org_id,
                    GSageDepartment.name == name,
                    GSageDepartment.id != dept_id,
                )
            )
        )
        if exists.scalar_one_or_none():
            raise DepartmentConflict(f"A department named '{name}' already exists in this org.")
        dept.name = name

    if slug is not None and slug != dept.slug:
        exists = await db.execute(
            select(GSageDepartment.id).where(
                and_(
                    GSageDepartment.org_id == org_id,
                    GSageDepartment.slug == slug,
                    GSageDepartment.id != dept_id,
                )
            )
        )
        if exists.scalar_one_or_none():
            raise DepartmentConflict(f"A department with slug '{slug}' already exists in this org.")
        dept.slug = slug

    if description is not None:
        dept.description = description
    if is_active is not None:
        dept.is_active = is_active

    await db.flush()
    await db.refresh(dept)
    return dept


async def delete_department(
    db: AsyncSession,
    dept_id: uuid.UUID,
    org_id: uuid.UUID,
) -> None:
    """Delete a department (and cascade to memberships).

    Raises
    ------
    DepartmentNotFound     — department doesn't exist.
    DepartmentDeleteError  — cannot delete the default department.
    """
    dept = await get_department(db, dept_id=dept_id, org_id=org_id)

    if dept.is_default:
        raise DepartmentDeleteError("The default department cannot be deleted.")

    await db.delete(dept)
    await db.flush()
    logger.info("Deleted department %s (org=%s)", dept_id, org_id)


# ---------------------------------------------------------------------------
# Membership operations
# ---------------------------------------------------------------------------


async def get_membership(
    db: AsyncSession,
    user_id: uuid.UUID,
    dept_id: uuid.UUID,
) -> Optional[GSageUserDepartment]:
    """Return a user's membership in a department, or None."""
    result = await db.execute(
        select(GSageUserDepartment).where(
            and_(
                GSageUserDepartment.user_id == user_id,
                GSageUserDepartment.dept_id == dept_id,
            )
        )
    )
    return result.scalar_one_or_none()


async def add_member(
    db: AsyncSession,
    dept_id: uuid.UUID,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = DepartmentRole.MEMBER,
) -> GSageUserDepartment:
    """Add a user to a department (or reactivate existing membership).

    Raises
    ------
    DepartmentNotFound   — department doesn't exist in this org.
    DepartmentConflict   — user already has an active membership.
    """
    await get_department(db, dept_id=dept_id, org_id=org_id)

    existing = await get_membership(db, user_id=user_id, dept_id=dept_id)
    if existing:
        if existing.is_active:
            raise DepartmentConflict("User is already a member of this department.")
        # Reactivate
        existing.is_active = True
        existing.role = role
        await db.flush()
        await db.refresh(existing)
        return existing

    membership = GSageUserDepartment(
        user_id=user_id,
        dept_id=dept_id,
        role=role,
        is_active=True,
    )
    db.add(membership)
    await db.flush()
    await db.refresh(membership)
    return membership


async def update_member(
    db: AsyncSession,
    dept_id: uuid.UUID,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    role: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> GSageUserDepartment:
    """Update a user's role or active state in a department.

    Raises
    ------
    DepartmentNotFound   — department or membership not found.
    """
    await get_department(db, dept_id=dept_id, org_id=org_id)

    membership = await get_membership(db, user_id=user_id, dept_id=dept_id)
    if membership is None:
        raise DepartmentNotFound("Membership not found.")

    if role is not None:
        membership.role = role
    if is_active is not None:
        membership.is_active = is_active

    await db.flush()
    await db.refresh(membership)
    return membership


async def remove_member(
    db: AsyncSession,
    dept_id: uuid.UUID,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Remove a user from a department.

    Raises
    ------
    DepartmentNotFound — membership not found.
    """
    await get_department(db, dept_id=dept_id, org_id=org_id)

    membership = await get_membership(db, user_id=user_id, dept_id=dept_id)
    if membership is None:
        raise DepartmentNotFound("Membership not found.")

    await db.delete(membership)
    await db.flush()


async def list_members(
    db: AsyncSession,
    dept_id: uuid.UUID,
    org_id: uuid.UUID,
    include_inactive: bool = False,
) -> Sequence[GSageUserDepartment]:
    """Return members of a department with user data eagerly loaded."""
    await get_department(db, dept_id=dept_id, org_id=org_id)

    filters = [GSageUserDepartment.dept_id == dept_id]
    if not include_inactive:
        filters.append(GSageUserDepartment.is_active.is_(True))

    result = await db.execute(
        select(GSageUserDepartment)
        .options(joinedload(GSageUserDepartment.user))
        .where(and_(*filters))
    )
    return result.unique().scalars().all()


async def get_user_departments(
    db: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
) -> Sequence[GSageUserDepartment]:
    """Return all active department memberships for a user within an org.

    Eagerly loads department info.
    """
    result = await db.execute(
        select(GSageUserDepartment)
        .join(
            GSageDepartment,
            GSageUserDepartment.dept_id == GSageDepartment.id,
        )
        .options(joinedload(GSageUserDepartment.department))
        .where(
            and_(
                GSageUserDepartment.user_id == user_id,
                GSageUserDepartment.is_active.is_(True),
                GSageDepartment.org_id == org_id,
                GSageDepartment.is_active.is_(True),
            )
        )
    )
    return result.unique().scalars().all()


async def ensure_user_in_default_department(
    db: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    role: str = DepartmentRole.MEMBER,
) -> GSageUserDepartment:
    """Add user to the default department if not already a member.

    Called during user onboarding and LDAP sync. Returns the membership row.
    Creates the default department if it doesn't exist yet (defensive).
    """
    default_dept = await get_default_department(db, org_id=org_id)
    if default_dept is None:
        default_dept = await create_default_department(db, org_id=org_id)

    existing = await get_membership(db, user_id=user_id, dept_id=default_dept.id)
    if existing:
        if not existing.is_active:
            existing.is_active = True
            await db.flush()
        return existing

    return await add_member(
        db=db,
        dept_id=default_dept.id,
        org_id=org_id,
        user_id=user_id,
        role=role,
    )
