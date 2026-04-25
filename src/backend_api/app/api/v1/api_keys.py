"""gSage AI — API key management routes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_db, get_org_membership, require_org_admin
from src.backend_api.app.schemas.api_keys import APIKeyCreate, APIKeyCreated, APIKeyOut, APIKeyRevoke
from src.backend_api.app.schemas.pagination import PaginatedResponse, PaginationParams, paginate_query
from src.shared.models.api_key import GSageAPIKey
from src.shared.models.user_organization import GSageUserOrganization
from src.shared.security.auth import calculate_api_key_expiration, generate_api_key

router = APIRouter()


@router.get("", response_model=PaginatedResponse[APIKeyOut], summary="List API keys for an org")
async def list_api_keys(
    org_id: uuid.UUID,
    pagination: Annotated[PaginationParams, Depends()],
    membership: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[APIKeyOut]:
    stmt = (
        select(GSageAPIKey)
        .where(GSageAPIKey.org_id == org_id)
        .order_by(GSageAPIKey.created_at.desc())
    )
    keys, total = await paginate_query(db, stmt, pagination)
    return PaginatedResponse.build(
        [APIKeyOut.model_validate(k) for k in keys],
        total=total,
        pagination=pagination,
    )


@router.post("", response_model=APIKeyCreated, status_code=status.HTTP_201_CREATED,
             summary="Create a new API key")
async def create_api_key(
    org_id: uuid.UUID,
    payload: APIKeyCreate,
    membership: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> APIKeyCreated:
    """Create an API key for the organization. The raw key is returned only once."""
    raw_key, key_hash, key_prefix = generate_api_key(payload.environment)
    expires_at = calculate_api_key_expiration(1)  # max 1 year

    api_key = GSageAPIKey(
        org_id=org_id,
        user_id=payload.user_id,
        name=payload.name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        environment=payload.environment,
        scoped_permissions=payload.scoped_permissions,
        interface=payload.interface,
        expires_at=expires_at,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        is_active=True,
        created_by_user_id=membership.user_id,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return APIKeyCreated(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        environment=api_key.environment,
        scoped_permissions=api_key.scoped_permissions,
        interface=api_key.interface,
        expires_at=api_key.expires_at,
        is_active=api_key.is_active,
        last_used_at=api_key.last_used_at,
        created_at=api_key.created_at,
        raw_key=raw_key,
    )


@router.get("/{key_id}", response_model=APIKeyOut, summary="Get a specific API key")
async def get_api_key(
    org_id: uuid.UUID,
    key_id: uuid.UUID,
    membership: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> APIKeyOut:
    result = await db.execute(
        select(GSageAPIKey).where(
            GSageAPIKey.id == key_id,
            GSageAPIKey.org_id == org_id,
        )
    )
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    return api_key  # type: ignore[return-value]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Revoke an API key")
async def revoke_api_key(
    org_id: uuid.UUID,
    key_id: uuid.UUID,
    membership: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    payload: Optional[APIKeyRevoke] = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(GSageAPIKey).where(
            GSageAPIKey.id == key_id,
            GSageAPIKey.org_id == org_id,
        )
    )
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    if api_key.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="API key already revoked")

    api_key.is_active = False
    api_key.revoked_at = datetime.now(timezone.utc)
    api_key.revoked_reason = payload.reason if payload else None
    await db.commit()


# ---------------------------------------------------------------------------
# Personal API key routes — any org member may manage their own keys
# ---------------------------------------------------------------------------

personal_router = APIRouter()


@personal_router.get("", response_model=list[APIKeyOut], summary="List my API keys")
async def list_my_api_keys(
    org_id: uuid.UUID,
    membership: Annotated[GSageUserOrganization, Depends(get_org_membership)],
    db: AsyncSession = Depends(get_db),
) -> list[APIKeyOut]:
    """List API keys that belong to the current user within the org."""
    result = await db.execute(
        select(GSageAPIKey)
        .where(
            GSageAPIKey.org_id == org_id,
            GSageAPIKey.user_id == membership.user_id,
        )
        .order_by(GSageAPIKey.created_at.desc())
    )
    return result.scalars().all()  # type: ignore[return-value]


@personal_router.post(
    "",
    response_model=APIKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create a personal API key",
)
async def create_my_api_key(
    org_id: uuid.UUID,
    payload: APIKeyCreate,
    membership: Annotated[GSageUserOrganization, Depends(get_org_membership)],
    db: AsyncSession = Depends(get_db),
) -> APIKeyCreated:
    """Create an API key scoped to the current user.  The raw key is returned only once."""
    raw_key, key_hash, key_prefix = generate_api_key(payload.environment)
    expires_at = calculate_api_key_expiration(1)

    api_key = GSageAPIKey(
        org_id=org_id,
        user_id=membership.user_id,
        name=payload.name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        environment=payload.environment,
        scoped_permissions=payload.scoped_permissions,
        interface=payload.interface,
        expires_at=expires_at,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        is_active=True,
        created_by_user_id=membership.user_id,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return APIKeyCreated(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        environment=api_key.environment,
        scoped_permissions=api_key.scoped_permissions,
        interface=api_key.interface,
        expires_at=api_key.expires_at,
        is_active=api_key.is_active,
        last_used_at=api_key.last_used_at,
        created_at=api_key.created_at,
        raw_key=raw_key,
    )


@personal_router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a personal API key",
)
async def revoke_my_api_key(
    org_id: uuid.UUID,
    key_id: uuid.UUID,
    membership: Annotated[GSageUserOrganization, Depends(get_org_membership)],
    payload: Optional[APIKeyRevoke] = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke one of the current user's own API keys."""
    result = await db.execute(
        select(GSageAPIKey).where(
            GSageAPIKey.id == key_id,
            GSageAPIKey.org_id == org_id,
            GSageAPIKey.user_id == membership.user_id,
        )
    )
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    if api_key.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="API key already revoked")

    api_key.is_active = False
    api_key.revoked_at = datetime.now(timezone.utc)
    api_key.revoked_reason = payload.reason if payload else None
    await db.commit()

