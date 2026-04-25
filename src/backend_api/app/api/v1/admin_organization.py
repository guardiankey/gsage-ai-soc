"""gSage AI — Admin: Organization settings endpoints.

Routes (prefix: /v1/orgs/{org_id}/admin):
    GET    /organization     Get organization details
    PATCH  /organization     Update organization settings
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_db, require_org_admin
from src.backend_api.app.schemas.admin import OrganizationOut, OrganizationUpdate
from src.shared.models.organization import GSageOrganization
from src.shared.models.user_organization import GSageUserOrganization

router = APIRouter()


@router.get(
    "/organization",
    response_model=OrganizationOut,
    summary="Get organization settings",
)
async def get_organization(
    org_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> OrganizationOut:
    """Return current organization settings. Admin/owner only."""
    result = await db.execute(
        select(GSageOrganization).where(GSageOrganization.id == org_id)
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    import json
    return OrganizationOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        is_active=org.is_active,
        system_prompt=org.system_prompt,
        default_maker_model=org.default_maker_model,
        default_reviewer_model=org.default_reviewer_model,
        agent_timeout_seconds=org.agent_timeout_seconds,
        max_context_tokens=org.max_context_tokens,
        llm_provider=org.llm_provider,
        llm_api_key_set=org._llm_api_key_encrypted is not None,
        auth_providers=json.loads(org._auth_providers_json),
        created_at=org.created_at,
        updated_at=org.updated_at,
    )


@router.patch(
    "/organization",
    response_model=OrganizationOut,
    summary="Update organization settings",
)
async def update_organization(
    org_id: uuid.UUID,
    payload: OrganizationUpdate,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> OrganizationOut:
    """Update organization settings. Admin/owner only."""
    import json

    result = await db.execute(
        select(GSageOrganization).where(GSageOrganization.id == org_id)
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    # Check name/slug uniqueness if changing
    update_data = payload.model_dump(exclude_unset=True)

    if "name" in update_data and update_data["name"] != org.name:
        clash = await db.execute(
            select(GSageOrganization).where(
                GSageOrganization.name == update_data["name"],
                GSageOrganization.id != org_id,
            )
        )
        if clash.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Organization name already in use")

    if "slug" in update_data and update_data["slug"] != org.slug:
        clash = await db.execute(
            select(GSageOrganization).where(
                GSageOrganization.slug == update_data["slug"],
                GSageOrganization.id != org_id,
            )
        )
        if clash.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Organization slug already in use")

    for field, value in update_data.items():
        if field == "llm_api_key":
            org.llm_api_key = value  # uses the model property (encrypts)
        else:
            setattr(org, field, value)

    await db.commit()
    await db.refresh(org)

    return OrganizationOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        is_active=org.is_active,
        system_prompt=org.system_prompt,
        default_maker_model=org.default_maker_model,
        default_reviewer_model=org.default_reviewer_model,
        agent_timeout_seconds=org.agent_timeout_seconds,
        max_context_tokens=org.max_context_tokens,
        llm_provider=org.llm_provider,
        llm_api_key_set=org._llm_api_key_encrypted is not None,
        auth_providers=json.loads(org._auth_providers_json),
        created_at=org.created_at,
        updated_at=org.updated_at,
    )
