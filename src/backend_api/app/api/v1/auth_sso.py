"""gSage AI — SSO routes (Microsoft Entra ID OIDC, Authorization Code + PKCE).

Flow
----
1. Browser hits ``GET /v1/auth/sso/{org_slug}/{provider}/start?next=...``.
   The backend generates ``state``, PKCE ``code_verifier``/``code_challenge``,
   and ``nonce``; persists them in Redis; then 302-redirects to the IdP's
   authorize endpoint.
2. The IdP redirects back to
   ``GET /v1/auth/sso/{org_slug}/{provider}/callback?code&state``.
   The backend consumes the saved state, exchanges the code, validates the
   id_token, upserts the local user, and 302-redirects to
   ``{public_base_url}/sso/complete?token={one_shot}&next=...``.
3. The web client POSTs the one-shot token to ``/v1/auth/sso/complete``,
   which returns the real :class:`TokenResponse` (access + refresh JWTs).

The one-shot indirection avoids putting the JWT in browser history or in
a query string visible to ad-tech / iframes.
"""

from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_db
from src.backend_api.app.schemas.auth import TokenResponse
from src.shared.auth import oidc_state
from src.shared.auth.backends.entra_oidc import EntraOIDCProvider
from src.shared.auth.guardiankey import GuardianKeyService
from src.shared.auth.registry import get_registry
from src.shared.auth.user_sync import upsert_external_user
from src.shared.config.settings import get_settings
from src.shared.models.organization import GSageOrganization
from src.shared.models.user_organization import GSageUserOrganization
from src.shared.models.user import GSageUser
from src.shared.security.auth import (
    create_access_token,
    create_refresh_token,
)
from src.backend_api.app.core.tenant import permissions_for_role

logger = logging.getLogger(__name__)


def _client_ip(request: Request) -> str:
    """Extract the client IP, honouring X-Forwarded-For when behind a proxy."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return ""


async def _gk_notify_failed(request: Request, username: str, reason: str) -> None:
    """Fire-and-forget notification to GuardianKey for an SSO failure.

    No-op when GuardianKey is disabled. Failures here are swallowed by the
    service (fail-open).
    """
    settings = get_settings()
    if not settings.gk_enabled:
        return
    client_ip = _client_ip(request)
    user_agent = request.headers.get("User-Agent", "")[:500]
    logger.info(
        "GuardianKey: SSO notify_event (failed) — user=%s ip=%s reason=%s",
        username, client_ip, reason,
    )
    await GuardianKeyService().notify_event(
        username, username, client_ip, user_agent, login_failed=1,
    )


async def _gk_check_or_block(request: Request, username: str) -> bool:
    """Run GuardianKey check_access for an SSO success.

    Returns ``True`` when the login is allowed (or GK is disabled / errored —
    fail-open), ``False`` when GK responded BLOCK and the caller must redirect
    to the login page with an error.
    """
    settings = get_settings()
    if not settings.gk_enabled:
        return True
    client_ip = _client_ip(request)
    user_agent = request.headers.get("User-Agent", "")[:500]
    logger.info(
        "GuardianKey: SSO check_access — user=%s ip=%s url=%s/v2/checkaccess",
        username, client_ip, settings.gk_api_url.rstrip("/"),
    )
    result = await GuardianKeyService().check_access(
        username, username, client_ip, user_agent,
    )
    logger.info(
        "GuardianKey: SSO check_access result — user=%s response=%s risk=%.3f "
        "should_block=%s should_notify=%s",
        username, result.response, result.risk,
        result.should_block, result.should_notify,
    )
    if result.should_notify:
        logger.warning(
            "GuardianKey SSO risk notification for '%s': response=%s risk=%.3f",
            username, result.response, result.risk,
        )
    return not result.should_block

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _public_base() -> str:
    return (get_settings().public_base_url or "").rstrip("/")


def _default_redirect_uri(org_slug: str, provider_name: str) -> str:
    return (
        f"{_public_base()}/api/v1/auth/sso/{org_slug}/{provider_name}/callback"
    )


def _safe_next(next_url: Optional[str]) -> str:
    """Sanitize the ``next`` URL.

    Only allow same-origin paths starting with a single ``/`` (and not ``//``)
    to prevent open-redirect.
    """
    if not next_url:
        return "/"
    if not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    return next_url


async def _load_org(db: AsyncSession, org_slug: str) -> GSageOrganization:
    res = await db.execute(
        select(GSageOrganization).where(GSageOrganization.slug == org_slug)
    )
    org = res.scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=404, detail="organization_not_found")
    return org


def _resolve_entra_provider() -> EntraOIDCProvider:
    registry = get_registry()
    provider = registry.get("entra_oidc")
    if not isinstance(provider, EntraOIDCProvider):
        raise HTTPException(
            status_code=503,
            detail="entra_oidc_provider_unavailable",
        )
    return provider


def _resolve_provider_config(org: GSageOrganization, provider_name: str) -> dict:
    cfg = (org.auth_config or {}).get(provider_name)
    if not cfg:
        raise HTTPException(
            status_code=404,
            detail=f"sso_provider_not_configured:{provider_name}",
        )
    return cfg


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SSOProvidersListItem(BaseModel):
    name: str
    display_name: str
    start_url: str


class SSOCompleteRequest(BaseModel):
    session_token: str = Field(..., min_length=20)


# ---------------------------------------------------------------------------
# GET /v1/auth/sso/{org_slug}/providers
# ---------------------------------------------------------------------------


@router.get(
    "/sso/{org_slug}/providers",
    response_model=list[SSOProvidersListItem],
    summary="List SSO providers configured for an org (public)",
)
async def list_sso_providers(
    org_slug: str,
    db: AsyncSession = Depends(get_db),
) -> list[SSOProvidersListItem]:
    org = await _load_org(db, org_slug)
    registry = get_registry()
    out: list[SSOProvidersListItem] = []
    for name in (org.auth_providers or []):
        if name == "local":
            continue
        provider = registry.get(name)
        if provider is None:
            continue
        cfg = (org.auth_config or {}).get(name) or {}
        if not cfg:
            continue
        out.append(
            SSOProvidersListItem(
                name=name,
                display_name=getattr(provider, "display_name", name),
                start_url=f"{_public_base()}/api/v1/auth/sso/{org.slug}/{name}/start",
            )
        )
    return out


# ---------------------------------------------------------------------------
# GET /v1/auth/sso/{org_slug}/{provider}/start
# ---------------------------------------------------------------------------


@router.get(
    "/sso/{org_slug}/{provider}/start",
    summary="Begin SSO authorization flow (browser redirect)",
)
async def sso_start(
    org_slug: str,
    provider: str,
    request: Request,
    next: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    if provider != "entra_oidc":
        raise HTTPException(status_code=404, detail="unknown_sso_provider")

    org = await _load_org(db, org_slug)
    config = _resolve_provider_config(org, provider)
    entra = _resolve_entra_provider()

    state = entra.generate_state()
    nonce = entra.generate_nonce()
    code_verifier, code_challenge = entra.generate_pkce_pair()

    redirect_uri = config.get("redirect_uri") or _default_redirect_uri(org.slug, provider)
    safe_next = _safe_next(next)

    saved = await oidc_state.save_state(
        state,
        {
            "org_id": str(org.id),
            "org_slug": org.slug,
            "provider": provider,
            "code_verifier": code_verifier,
            "nonce": nonce,
            "redirect_uri": redirect_uri,
            "next": safe_next,
        },
    )
    if not saved:
        raise HTTPException(
            status_code=503, detail="oidc_state_store_unavailable"
        )

    try:
        authorize_url = await entra.build_authorize_url(
            config,
            state=state,
            code_challenge=code_challenge,
            nonce=nonce,
            redirect_uri=redirect_uri,
        )
    except Exception as exc:
        logger.error("sso_start: build_authorize_url failed — %s", exc)
        raise HTTPException(status_code=502, detail="oidc_discovery_failed") from exc

    return RedirectResponse(url=authorize_url, status_code=302)


# ---------------------------------------------------------------------------
# GET /v1/auth/sso/{org_slug}/{provider}/callback
# ---------------------------------------------------------------------------


@router.get(
    "/sso/{org_slug}/{provider}/callback",
    summary="OIDC authorization-code callback",
)
async def sso_callback(
    org_slug: str,
    provider: str,
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    if provider != "entra_oidc":
        raise HTTPException(status_code=404, detail="unknown_sso_provider")

    if error:
        logger.warning(
            "sso_callback: IdP returned error %s — %s", error, error_description
        )
        # IdP-side failure — we usually don't have the user identity here, so
        # the GK notify uses the error code as the username placeholder.
        await _gk_notify_failed(request, f"sso:{error}", reason="idp_error")
        return RedirectResponse(
            url=f"{_public_base()}/login?sso_error={error}", status_code=302
        )

    if not code or not state:
        raise HTTPException(status_code=400, detail="missing_code_or_state")

    saved = await oidc_state.consume_state(state)
    if saved is None:
        raise HTTPException(status_code=400, detail="invalid_or_expired_state")

    if saved.get("org_slug") != org_slug or saved.get("provider") != provider:
        raise HTTPException(status_code=400, detail="state_mismatch")

    org = await _load_org(db, org_slug)
    config = _resolve_provider_config(org, provider)
    entra = _resolve_entra_provider()

    auth_result = await entra.exchange_code(
        config,
        code=code,
        code_verifier=saved["code_verifier"],
        nonce=saved["nonce"],
        redirect_uri=saved["redirect_uri"],
    )

    if not auth_result.success or auth_result.identity is None:
        logger.warning(
            "sso_callback: exchange failed — %s / %s",
            auth_result.error_type, auth_result.error_message,
        )
        await _gk_notify_failed(
            request,
            (auth_result.identity.email if auth_result.identity else "sso:exchange_failed"),
            reason="exchange_failed",
        )
        return RedirectResponse(
            url=f"{_public_base()}/login?sso_error=auth_failed", status_code=302
        )

    auth_result.provider_name = provider

    # auto_provision_users gate
    if not bool(config.get("auto_provision_users", True)):
        existing = await db.execute(
            select(GSageUser).where(GSageUser.email == auth_result.identity.email)
        )
        if existing.scalar_one_or_none() is None:
            logger.info(
                "sso_callback: refusing unknown user %s (auto_provision_users=False)",
                auth_result.identity.email,
            )
            await _gk_notify_failed(
                request, auth_result.identity.email, reason="user_not_provisioned",
            )
            return RedirectResponse(
                url=f"{_public_base()}/login?sso_error=user_not_provisioned",
                status_code=302,
            )

    user, membership = await upsert_external_user(db, org, auth_result, config)
    await db.commit()

    # GuardianKey post-credential risk check. BLOCK → redirect to login with
    # a generic error (no info leak about whether the account exists or is
    # restricted). NOTIFY/HARD_NOTIFY/ERROR → proceed (logged inside helper).
    if not await _gk_check_or_block(request, user.email):
        logger.warning(
            "sso_callback: GuardianKey blocked SSO login for %s", user.email,
        )
        return RedirectResponse(
            url=f"{_public_base()}/login?sso_error=blocked", status_code=302,
        )

    # Issue a one-shot session token; the SPA exchanges it for the real JWTs.
    session_token = secrets.token_urlsafe(32)
    await oidc_state.save_session_token(
        session_token,
        {
            "user_id": str(user.id),
            "org_id": str(membership.org_id),
            "role": membership.role,
            "email": user.email,
            "must_change_password": False,
        },
    )

    safe_next = _safe_next(saved.get("next"))
    redirect = (
        f"{_public_base()}/sso/complete"
        f"?token={session_token}&next={safe_next}"
    )
    return RedirectResponse(url=redirect, status_code=302)


# ---------------------------------------------------------------------------
# POST /v1/auth/sso/complete
# ---------------------------------------------------------------------------


@router.post(
    "/sso/complete",
    response_model=TokenResponse,
    summary="Exchange a one-shot SSO session token for JWT tokens",
)
async def sso_complete(
    body: SSOCompleteRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    payload = await oidc_state.consume_session_token(body.session_token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_or_expired_session_token",
        )

    user_id = payload["user_id"]
    org_id = payload["org_id"]
    role = payload["role"]
    email = payload["email"]

    # Re-validate that the membership is still active.
    res = await db.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == user_id,
            GSageUserOrganization.org_id == org_id,
            GSageUserOrganization.is_active == True,  # noqa: E712
        )
    )
    membership = res.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=403, detail="membership_inactive")

    token_data = {
        "sub": user_id,
        "email": email,
        "org_id": org_id,
        "org_role": role,
        "permissions": permissions_for_role(role),
    }

    return TokenResponse(
        access_token=create_access_token(token_data),
        refresh_token=create_refresh_token({"sub": user_id, "org_id": org_id}),
    )
