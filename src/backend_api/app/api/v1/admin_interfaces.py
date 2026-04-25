"""gSage AI — Admin: Interface profile endpoints.

Routes (prefix: /v1/orgs/{org_id}/admin):
    GET    /interface-profiles                       List profiles
    POST   /interface-profiles                       Create profile
    GET    /interface-profiles/{profile_id}          Get profile detail
    PATCH  /interface-profiles/{profile_id}          Update profile
    DELETE /interface-profiles/{profile_id}          Delete profile
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_db, require_org_admin
from src.backend_api.app.schemas.admin import (
    InterfaceProfileCreate,
    InterfaceProfileOut,
    InterfaceProfileUpdate,
)
from src.shared.cache.permissions_cache import get_perm_redis_client, invalidate_org_permissions
from src.shared.models.interface_profile import GSageInterfaceProfile
from src.shared.models.user_organization import GSageUserOrganization

router = APIRouter()


@router.get(
    "/interface-profiles",
    response_model=list[InterfaceProfileOut],
    summary="List interface profiles",
)
async def list_interface_profiles(
    org_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
    interface: str | None = None,
    dept_id: uuid.UUID | None = None,
) -> list[InterfaceProfileOut]:
    """List all interface profiles for the org. Optional filters: ``interface``, ``dept_id``."""
    stmt = select(GSageInterfaceProfile).where(
        GSageInterfaceProfile.org_id == org_id
    )
    if interface:
        stmt = stmt.where(GSageInterfaceProfile.interface == interface)
    if dept_id is not None:
        stmt = stmt.where(GSageInterfaceProfile.dept_id == dept_id)
    stmt = stmt.order_by(GSageInterfaceProfile.interface, GSageInterfaceProfile.created_at)

    result = await db.execute(stmt)
    return [InterfaceProfileOut.model_validate(p) for p in result.scalars().all()]


@router.post(
    "/interface-profiles",
    response_model=InterfaceProfileOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create interface profile",
)
async def create_interface_profile(
    org_id: uuid.UUID,
    payload: InterfaceProfileCreate,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> InterfaceProfileOut:
    """Create a new interface profile."""
    profile = GSageInterfaceProfile(
        org_id=org_id,
        dept_id=payload.dept_id,
        interface=payload.interface,
        user_id=payload.user_id,
        is_active=payload.is_active,
        description=payload.description,
        system_prompt=payload.system_prompt,
        mode=payload.mode,
        tool_permissions=payload.tool_permissions,
        interface_config=payload.interface_config,
        preferences=payload.preferences,
    )
    db.add(profile)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An interface profile for this (org, dept, interface, user) already exists",
        )
    await db.refresh(profile)

    # Invalidate permission cache for entire org (new profile may filter tools)
    rc = get_perm_redis_client()
    if rc is not None:
        await invalidate_org_permissions(rc, org_id)

    return InterfaceProfileOut.model_validate(profile)


@router.get(
    "/interface-profiles/{profile_id}",
    response_model=InterfaceProfileOut,
    summary="Get interface profile",
)
async def get_interface_profile(
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> InterfaceProfileOut:
    result = await db.execute(
        select(GSageInterfaceProfile).where(
            GSageInterfaceProfile.id == profile_id,
            GSageInterfaceProfile.org_id == org_id,
        )
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interface profile not found")
    return InterfaceProfileOut.model_validate(profile)


@router.patch(
    "/interface-profiles/{profile_id}",
    response_model=InterfaceProfileOut,
    summary="Update interface profile",
)
async def update_interface_profile(
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    payload: InterfaceProfileUpdate,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> InterfaceProfileOut:
    result = await db.execute(
        select(GSageInterfaceProfile).where(
            GSageInterfaceProfile.id == profile_id,
            GSageInterfaceProfile.org_id == org_id,
        )
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interface profile not found")

    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(profile, key, value)

    await db.commit()
    await db.refresh(profile)

    # Invalidate permission cache for entire org
    rc = get_perm_redis_client()
    if rc is not None:
        await invalidate_org_permissions(rc, org_id)

    return InterfaceProfileOut.model_validate(profile)


@router.delete(
    "/interface-profiles/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete interface profile",
)
async def delete_interface_profile(
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(GSageInterfaceProfile).where(
            GSageInterfaceProfile.id == profile_id,
            GSageInterfaceProfile.org_id == org_id,
        )
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interface profile not found")

    await db.delete(profile)
    await db.commit()

    # Invalidate permission cache for entire org
    rc = get_perm_redis_client()
    if rc is not None:
        await invalidate_org_permissions(rc, org_id)
