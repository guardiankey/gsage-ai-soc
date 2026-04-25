"""gSage AI — Department REST endpoints.

Routes
------
Department endpoints (prefix: /v1/orgs/{org_id}/depts):
    GET    /                              List departments in the org
    POST   /                              Create a new department (admin/owner only)
    GET    /{dept_id}                     Get department detail
    PATCH  /{dept_id}                     Update department (admin/owner or dept admin)
    DELETE /{dept_id}                     Delete department (admin/owner only)

Member endpoints (prefix: /v1/orgs/{org_id}/depts/{dept_id}/members):
    GET    /                              List members of a department
    POST   /                              Add a user to a department
    PATCH  /{user_id}                     Update a member's role
    DELETE /{user_id}                     Remove a user from a department

User membrane (prefix: /v1/orgs/{org_id}/my-depts):
    GET    /                              Return the current user's departments in this org
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_department_context, get_tenant_context, require_org_admin
from src.backend_api.app.core.tenant import TenantContext
from src.backend_api.app.schemas.department import (
    DepartmentCreate,
    DepartmentOut,
    DepartmentUpdate,
    DeptMemberAdd,
    DeptMemberOut,
    DeptMemberUpdate,
)
from src.shared.database import get_db
from src.shared.services.department_service import (
    DepartmentConflict,
    DepartmentDeleteError,
    DepartmentNotFound,
    add_member,
    create_department,
    delete_department,
    get_department,
    get_user_departments,
    list_departments,
    list_members,
    remove_member,
    update_department,
    update_member,
)

router = APIRouter()

# ---------------------------------------------------------------------------
# Department CRUD
# ---------------------------------------------------------------------------


@router.get("/", response_model=list[DepartmentOut])
async def list_org_departments(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
    include_inactive: bool = False,
) -> list[DepartmentOut]:
    """List all departments in the org. Members see only active ones."""
    if include_inactive and ctx.org_role not in ("owner", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required to see inactive departments",
        )
    depts = await list_departments(db, org_id=ctx.org_id, include_inactive=include_inactive)
    return [DepartmentOut.model_validate(d) for d in depts]


@router.post("/", response_model=DepartmentOut, status_code=status.HTTP_201_CREATED)
async def create_org_department(
    payload: DepartmentCreate,
    _: Annotated[None, Depends(require_org_admin)],
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
) -> DepartmentOut:
    """Create a new department. Org admin/owner only."""
    try:
        dept = await create_department(
            db=db,
            org_id=ctx.org_id,
            name=payload.name,
            slug=payload.slug,
            description=payload.description,
        )
        await db.commit()
        return DepartmentOut.model_validate(dept)
    except DepartmentConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/{dept_id}", response_model=DepartmentOut)
async def get_org_department(
    dept_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
) -> DepartmentOut:
    """Get a single department."""
    try:
        dept = await get_department(db, dept_id=dept_id, org_id=ctx.org_id)
    except DepartmentNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")
    return DepartmentOut.model_validate(dept)


@router.patch("/{dept_id}", response_model=DepartmentOut)
async def update_org_department(
    dept_id: uuid.UUID,
    payload: DepartmentUpdate,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: AsyncSession = Depends(get_db),
) -> DepartmentOut:
    """Update a department. Dept admin (or org admin/owner) only."""
    ctx.require_dept_admin()
    try:
        dept = await update_department(
            db=db,
            dept_id=dept_id,
            org_id=ctx.org_id,
            name=payload.name,
            slug=payload.slug,
            description=payload.description,
            is_active=payload.is_active,
        )
        await db.commit()
        return DepartmentOut.model_validate(dept)
    except DepartmentNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")
    except DepartmentConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.delete("/{dept_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_org_department(
    dept_id: uuid.UUID,
    _: Annotated[None, Depends(require_org_admin)],
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a department (org admin/owner only). Cannot delete the default department."""
    try:
        await delete_department(db, dept_id=dept_id, org_id=ctx.org_id)
        await db.commit()
    except DepartmentNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")
    except DepartmentDeleteError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


# ---------------------------------------------------------------------------
# Member management
# ---------------------------------------------------------------------------


@router.get("/{dept_id}/members", response_model=list[DeptMemberOut])
async def list_dept_members(
    dept_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: AsyncSession = Depends(get_db),
    include_inactive: bool = False,
) -> list[DeptMemberOut]:
    """List members of a department."""
    members = await list_members(db, dept_id=dept_id, org_id=ctx.org_id, include_inactive=include_inactive)
    result = []
    for m in members:
        out = DeptMemberOut.model_validate(m)
        if m.user:
            out = out.model_copy(update={"user_email": m.user.email, "user_full_name": m.user.full_name})
        result.append(out)
    return result


@router.post("/{dept_id}/members", response_model=DeptMemberOut, status_code=status.HTTP_201_CREATED)
async def add_dept_member(
    dept_id: uuid.UUID,
    payload: DeptMemberAdd,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: AsyncSession = Depends(get_db),
) -> DeptMemberOut:
    """Add a user to a department. Dept admin (or org admin/owner) only."""
    ctx.require_dept_admin()
    try:
        membership = await add_member(
            db=db,
            dept_id=dept_id,
            org_id=ctx.org_id,
            user_id=payload.user_id,
            role=payload.role,
        )
        await db.commit()
        return DeptMemberOut.model_validate(membership)
    except DepartmentNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")
    except DepartmentConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.patch("/{dept_id}/members/{user_id}", response_model=DeptMemberOut)
async def update_dept_member(
    dept_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: DeptMemberUpdate,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: AsyncSession = Depends(get_db),
) -> DeptMemberOut:
    """Update a member's role or active state. Dept admin (or org admin/owner) only."""
    ctx.require_dept_admin()
    try:
        membership = await update_member(
            db=db,
            dept_id=dept_id,
            org_id=ctx.org_id,
            user_id=user_id,
            role=payload.role,
            is_active=payload.is_active,
        )
        await db.commit()
        return DeptMemberOut.model_validate(membership)
    except DepartmentNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found")


@router.delete("/{dept_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_dept_member(
    dept_id: uuid.UUID,
    user_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_department_context)],
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a user from a department. Dept admin (or org admin/owner) only."""
    ctx.require_dept_admin()
    try:
        await remove_member(db=db, dept_id=dept_id, org_id=ctx.org_id, user_id=user_id)
        await db.commit()
    except DepartmentNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found")


# ---------------------------------------------------------------------------
# User's own departments
# ---------------------------------------------------------------------------


@router.get("/my", response_model=list[DeptMemberOut])
async def my_departments(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
) -> list[DeptMemberOut]:
    """Return the current user's active department memberships within this org."""
    memberships = await get_user_departments(db, user_id=ctx.user_id, org_id=ctx.org_id)
    result = []
    for m in memberships:
        out = DeptMemberOut.model_validate(m)
        if m.department:
            out = out.model_copy(update={"dept_id": m.department.id})
        result.append(out)
    return result
