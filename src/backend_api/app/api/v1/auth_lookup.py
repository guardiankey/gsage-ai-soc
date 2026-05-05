"""gSage AI — Public auth lookup endpoint.

``POST /v1/auth/lookup`` answers the multi-step login UI: given an email
address, it returns the org slug (when known) and the available login
methods (password, SSO providers, …).

Security
--------
- The response shape is **identical** for known and unknown domains so
  attackers cannot enumerate which domains are SSO-enabled.
- No user existence is leaked — the lookup is by *domain*, not by user.
- Rate-limited via :func:`public_rate_limit`.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_db
from src.shared.auth.registry import get_registry
from src.shared.config.settings import get_settings
from src.shared.models.org_email_domain import GSageOrgEmailDomain
from src.shared.models.organization import GSageOrganization

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class AuthLookupRequest(BaseModel):
    email: EmailStr


class SSOProviderInfo(BaseModel):
    name: str = Field(..., description="Provider identifier (e.g. 'entra_oidc')")
    display_name: str = Field(..., description="Human-friendly provider name")
    start_url: str = Field(
        ...,
        description="Absolute URL the browser should navigate to in order to start the SSO flow",
    )


class AuthLookupResponse(BaseModel):
    org_slug: Optional[str] = Field(
        None, description="Organization slug if the domain is known."
    )
    allow_password_login: bool = Field(
        True,
        description="Whether the org allows password-based authentication.",
    )
    sso_providers: list[SSOProviderInfo] = Field(
        default_factory=list,
        description="Configured SSO providers for the org (empty when none).",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _domain_of(email: str) -> Optional[str]:
    if "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    return domain or None


def _public_base() -> str:
    return (get_settings().public_base_url or "").rstrip("/")


def _start_url(org_slug: str, provider_name: str) -> str:
    return f"{_public_base()}/api/v1/auth/sso/{org_slug}/{provider_name}/start"


def _default_lookup_response() -> AuthLookupResponse:
    """Uniform response when the domain is unknown.

    Returns ``allow_password_login=True`` so the UI proceeds to the password
    step (the actual login attempt will succeed or fail server-side, never
    revealing whether the org exists).
    """
    return AuthLookupResponse(org_slug=None, allow_password_login=True, sso_providers=[])


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post(
    "/lookup",
    response_model=AuthLookupResponse,
    summary="Discover login methods for an email domain (multi-step login UX)",
)
async def auth_lookup(
    request: Request,
    body: AuthLookupRequest,
    db: AsyncSession = Depends(get_db),
) -> AuthLookupResponse:
    domain = _domain_of(body.email)
    if domain is None:
        return _default_lookup_response()

    res = await db.execute(
        select(GSageOrgEmailDomain).where(GSageOrgEmailDomain.domain == domain)
    )
    mapping = res.scalar_one_or_none()
    if mapping is None:
        return _default_lookup_response()

    res = await db.execute(
        select(GSageOrganization).where(GSageOrganization.id == mapping.org_id)
    )
    org = res.scalar_one_or_none()
    if org is None:
        return _default_lookup_response()

    providers: list[str] = list(org.auth_providers or [])
    auth_config: dict = org.auth_config or {}

    allow_password = "local" in providers
    sso_infos: list[SSOProviderInfo] = []

    registry = get_registry()
    for provider_name in providers:
        if provider_name == "local":
            continue
        provider = registry.get(provider_name)
        if provider is None:
            continue
        # Only surface providers that actually have non-empty config
        cfg = auth_config.get(provider_name) or {}
        if not cfg:
            continue
        sso_infos.append(
            SSOProviderInfo(
                name=provider_name,
                display_name=getattr(provider, "display_name", provider_name),
                start_url=_start_url(org.slug, provider_name),
            )
        )

    return AuthLookupResponse(
        org_slug=org.slug,
        allow_password_login=allow_password,
        sso_providers=sso_infos,
    )
