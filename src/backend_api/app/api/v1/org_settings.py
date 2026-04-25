"""gSage AI — organization settings endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_db, require_org_admin
from src.backend_api.app.schemas.auth import OrgOTPConfigRequest, OrgOTPConfigResponse
from src.shared.models.organization import GSageOrganization
from src.shared.models.user_organization import GSageUserOrganization

router = APIRouter()

_DEFAULT_OTP_CONFIG = {
    "policy": "optional",
    "trusted_networks": [],
    "remember_device_days": 30,
    "issuer_name": "gSage AI",
}


def _get_otp_config(org: GSageOrganization) -> dict:
    return {**_DEFAULT_OTP_CONFIG, **org.auth_config.get("otp", {})}


@router.get(
    "/orgs/{org_id}/settings/otp",
    response_model=OrgOTPConfigResponse,
    summary="Get OTP configuration for an organization",
)
async def get_org_otp_config(
    org_id: uuid.UUID,
    _membership: GSageUserOrganization = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
) -> OrgOTPConfigResponse:
    """Return the current OTP/TOTP policy configuration for an organization.

    Requires admin or owner role within the organization.
    """
    result = await db.execute(
        select(GSageOrganization).where(GSageOrganization.id == org_id)
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    cfg = _get_otp_config(org)
    return OrgOTPConfigResponse(**cfg)


@router.put(
    "/orgs/{org_id}/settings/otp",
    response_model=OrgOTPConfigResponse,
    summary="Update OTP configuration for an organization",
)
async def update_org_otp_config(
    org_id: uuid.UUID,
    body: OrgOTPConfigRequest,
    _membership: GSageUserOrganization = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
) -> OrgOTPConfigResponse:
    """Update the OTP/TOTP policy configuration for an organization.

    Only the supplied fields are updated; others keep their current values.
    Requires admin or owner role within the organization.
    """
    result = await db.execute(
        select(GSageOrganization).where(GSageOrganization.id == org_id)
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    current_config = org.auth_config
    otp_config = {**_DEFAULT_OTP_CONFIG, **current_config.get("otp", {})}

    # Apply only the provided fields
    update_data = body.model_dump(exclude_none=True)
    otp_config.update(update_data)

    current_config["otp"] = otp_config
    org.auth_config = current_config
    await db.commit()

    return OrgOTPConfigResponse(**otp_config)
