"""gSage AI — authentication routes.

Routes
------
POST /token                          Login — OAuth2 form (get JWT tokens)
POST /login                          Login — JSON body (get JWT tokens)
POST /register                       Register — create user + first org
POST /refresh                        Refresh access token
GET  /me                             Current user profile
PATCH /me                            Update current user profile (full_name)
POST /me/change-password             Change current user password

POST /otp/verify                     Complete OTP verification step (after otp_required=true)
POST /otp/setup                      Start OTP enrollment (generate secret + QR)
POST /otp/confirm                    Confirm OTP enrollment with first valid TOTP code
DELETE /otp                          Disable OTP for the current user
POST /otp/backup-codes/regenerate    Regenerate OTP backup codes
GET  /otp/status                     OTP enrollment status for the current user

Auth flow
---------
Standard login: POST /login → returns access + refresh tokens.
OTP login: POST /login → returns ``otp_required=true`` + ``otp_token``
  → POST /otp/verify with code → returns access + refresh tokens.
Token refresh: POST /refresh → new access + refresh token pair.

Multi-tenant
------------
Every login resolves the target ``GSageOrganization`` via ``_resolve_org_for_login``.
If ``org_id`` is supplied in the request body, that org is used directly (required for
external-auth users not yet provisioned locally).  Otherwise the user's first active
membership is chosen.

Permissions are derived from the user's role via ``permissions_for_role`` and embedded
in the JWT claims so downstream services can enforce RBAC without extra DB lookups.
"""

from __future__ import annotations

import re
from typing import Any, cast
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.backend_api.app.api.deps import get_current_active_user, get_db
from src.backend_api.app.core.tenant import permissions_for_role
from src.backend_api.app.schemas.auth import (
    ChangePasswordRequest,
    DepartmentMembershipOut,
    LoginRequest,
    MeResponse,
    OrgMembershipOut,
    OTPConfirmRequest,
    OTPConfirmResponse,
    OTPDisableRequest,
    OTPSetupResponse,
    OTPStatusResponse,
    OTPVerifyRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UpdateProfileRequest,
)
from src.shared.models.department import GSageDepartment
from src.shared.models.organization import GSageOrganization
from src.shared.models.trusted_device import GSageTrustedDevice
from src.shared.models.user import GSageUser
from src.shared.models.user_department import GSageUserDepartment
from src.shared.models.user_organization import GSageUserOrganization
from src.shared.security.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from src.shared.config.settings import get_settings
from src.shared.auth.guardiankey import GuardianKeyService
from src.shared.services.otp_service import OTPRequirement, OTPService, resolve_otp_requirement

router = APIRouter()

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _slug_from_name(name: str) -> str:
    """Derive a URL-safe slug from an org name."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:100] or "org"


def _build_token_data(
    user: GSageUser,
    membership: GSageUserOrganization,
) -> dict:
    """Build the full JWT claims dict for an access token."""
    return {
        "sub": str(user.id),
        "email": user.email,
        "org_id": str(membership.org_id),
        "org_role": membership.role,
        "permissions": permissions_for_role(membership.role),
    }


async def _get_membership_for_login(
    user: GSageUser,
    org_id: uuid.UUID | None,
    db: AsyncSession,
) -> GSageUserOrganization:
    """Return the active membership to use for token generation.

    If *org_id* is provided, validate that the user belongs to it.
    Otherwise, return the first active membership (sorted by created_at).
    """
    if org_id is not None:
        result = await db.execute(
            select(GSageUserOrganization).where(
                GSageUserOrganization.user_id == user.id,
                GSageUserOrganization.org_id == org_id,
                GSageUserOrganization.is_active == True,  # noqa: E712
            )
        )
        membership = result.scalar_one_or_none()
        if membership is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not an active member of the specified organization",
            )
        return membership

    # No org_id — pick the first active org
    result = await db.execute(
        select(GSageUserOrganization)
        .where(
            GSageUserOrganization.user_id == user.id,
            GSageUserOrganization.is_active == True,  # noqa: E712
        )
        .order_by(GSageUserOrganization.created_at)
        .limit(1)
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not belong to any active organization",
        )
    return membership


async def _resolve_org_for_login(
    org_id: uuid.UUID | None,
    existing_user: GSageUser | None,
    db: AsyncSession,
) -> GSageOrganization:
    """Resolve the GSageOrganization to use for the login chain.

    If *org_id* is provided it is loaded directly (supports external-auth users
    who are not yet in the DB).  Otherwise, the user must already exist and
    their first active organisation is used.
    """
    if org_id is not None:
        result = await db.execute(
            select(GSageOrganization).where(
                GSageOrganization.id == org_id,
                GSageOrganization.is_active == True,  # noqa: E712
            )
        )
        org = result.scalar_one_or_none()
        if org is None:
            # Deliberately vague — do not disclose whether the org_id exists
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return org

    # No org_id — user must be in the DB so we can find their org
    if existing_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(
        select(GSageOrganization)
        .join(
            GSageUserOrganization,
            GSageOrganization.id == GSageUserOrganization.org_id,
        )
        .where(
            GSageUserOrganization.user_id == existing_user.id,
            GSageUserOrganization.is_active == True,  # noqa: E712
            GSageOrganization.is_active == True,  # noqa: E712
        )
        .order_by(GSageUserOrganization.created_at)
        .limit(1)
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not belong to any active organization",
        )
    return org


async def _run_auth_chain(
    username: str,
    password: str,
    org: GSageOrganization,
    existing_user: GSageUser | None,
    db: AsyncSession,
) -> tuple:
    """Run the org's auth provider chain and return (AuthResult, GSageUser, GSageUserOrganization).

    For the ``local`` provider, pre-fetched credentials from *existing_user* are
    injected into the config dict (avoids a second DB round-trip inside the provider).

    Raises HTTPException 401/403 on authentication failure.
    """
    from src.shared.auth import get_registry
    from src.shared.auth.user_sync import upsert_external_user

    registry = get_registry()
    providers = org.auth_providers
    base_config = org.auth_config  # per-org overrides (decrypted)

    # Build per-provider config, starting from org-level overrides
    provider_configs: dict[str, dict] = {p: dict(base_config.get(p, {})) for p in providers}

    # Inject local auth data so LocalAuthProvider needs no DB access
    if "local" in providers and existing_user is not None and existing_user.password_hash:
        provider_configs["local"].update({
            "_password_hash": existing_user.password_hash,
            "_email": existing_user.email,
            "_full_name": existing_user.full_name,
        })

    auth_result = await registry.authenticate_chain(
        providers, provider_configs, username, password
    )

    if not auth_result.success:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect credentials or account is restricted",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Resolve user + membership based on which provider authenticated
    if auth_result.provider_name != "local":
        # External provider — auto-provision user and sync group memberships
        provider_config = provider_configs.get(auth_result.provider_name, {})
        user, membership = await upsert_external_user(db, org, auth_result, provider_config)
    else:
        # Local provider — user must already exist in the DB
        if existing_user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = existing_user
        membership = await _get_membership_for_login(user, org.id, db)

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user",
        )

    return auth_result, user, membership


def _create_otp_token(user: GSageUser, membership: GSageUserOrganization) -> str:
    """Create a short-lived JWT for the OTP verification step (5 minutes)."""
    return create_access_token(
        {
            "sub": str(user.id),
            "org_id": str(membership.org_id),
            "org_role": membership.role,
            "purpose": "otp_verify",
            "type": "otp",
        },
        expires_delta=timedelta(minutes=5),
    )


async def _verify_otp_token(token: str, db: AsyncSession) -> tuple[GSageUser, GSageUserOrganization]:
    """Decode an otp_token and return (user, membership). Raises 401 on failure."""
    try:
        payload = decode_token(token)
        if payload.get("type") != "otp" or payload.get("purpose") != "otp_verify":
            raise ValueError("Not an OTP token")
        user_id = uuid.UUID(payload["sub"])
        org_id = uuid.UUID(payload["org_id"])
    except (ValueError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired OTP token",
        )
    result = await db.execute(select(GSageUser).where(GSageUser.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    membership = await _get_membership_for_login(user, org_id, db)
    return user, membership


def _client_ip(request: Request) -> Optional[str]:
    """Extract client IP from X-Forwarded-For or direct connection."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


async def _guardiankey_check(
    request: Request,
    username: str,
    login_failed: bool = False,
) -> None:
    """Post-credential GuardianKey risk check.

    - ``login_failed=True``: notify GK of the failed attempt (fire-and-forget).
    - ``login_failed=False``: check access and raise 401 if GK responds BLOCK.
    NOTIFY/HARD_NOTIFY responses allow login but emit a warning log.
    Always fail-open: if GK is disabled or unreachable, this is a no-op.
    """
    import logging as _log
    _logger = _log.getLogger(__name__)

    settings = get_settings()
    if not settings.gk_enabled:
        _logger.debug("GuardianKey: disabled (gk_enabled=false), skipping")
        return

    client_ip = _client_ip(request) or ""
    user_agent = request.headers.get("User-Agent", "")[:500]

    # Surface configuration gaps loudly — with gk_enabled=true but missing
    # credentials the API call would fail silently (fail-open) and look
    # exactly like "GuardianKey not integrated".
    missing = [
        name for name, value in (
            ("GK_ORG_ID", settings.gk_org_id),
            ("GK_AUTHGROUP_ID", settings.gk_authgroup_id),
            ("GK_KEY", settings.gk_key),
            ("GK_IV", settings.gk_iv),
        ) if not value
    ]
    if missing:
        _logger.warning(
            "GuardianKey: enabled but missing required settings %s — "
            "requests will be rejected by the API. Check .env on backend_api.",
            missing,
        )

    gk = GuardianKeyService()

    if login_failed:
        _logger.info(
            "GuardianKey: notify_event (failed login) — user=%s ip=%s url=%s/v2/checkaccess",
            username, client_ip, settings.gk_api_url.rstrip("/"),
        )
        await gk.notify_event(username, username, client_ip, user_agent, login_failed=1)
        return

    _logger.info(
        "GuardianKey: check_access — user=%s ip=%s url=%s/v2/checkaccess",
        username, client_ip, settings.gk_api_url.rstrip("/"),
    )
    result = await gk.check_access(username, username, client_ip, user_agent)
    _logger.info(
        "GuardianKey: check_access result — user=%s response=%s risk=%.3f "
        "should_block=%s should_notify=%s",
        username, result.response, result.risk,
        result.should_block, result.should_notify,
    )
    if result.should_notify:
        _logger.warning(
            "GuardianKey risk notification for '%s': response=%s risk=%.3f",
            username, result.response, result.risk,
        )
    if result.should_block:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect credentials or account is restricted",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/token", response_model=TokenResponse, summary="Login — OAuth2 form (get JWT tokens)")
async def login_form(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Authenticate with email + password via OAuth2 form and receive JWT tokens.

    If OTP is enabled, returns ``otp_required=true`` and a short-lived ``otp_token``.
    The client must then call ``POST /auth/otp/verify`` to complete login.
    """
    result = await db.execute(
        select(GSageUser).where(GSageUser.email == form_data.username)
    )
    existing_user = result.scalar_one_or_none()

    org = await _resolve_org_for_login(None, existing_user, db)
    try:
        auth_result, user, membership = await _run_auth_chain(
            form_data.username, form_data.password, org, existing_user, db
        )
    except HTTPException:
        await _guardiankey_check(request, form_data.username, login_failed=True)
        raise

    await _guardiankey_check(request, form_data.username, login_failed=False)

    otp_requirement = await resolve_otp_requirement(
        org, user, _client_ip(request), request.headers.get("X-Device-Token"), db
    )

    if otp_requirement in (OTPRequirement.REQUIRED, OTPRequirement.NOT_ENROLLED):
        return TokenResponse(
            access_token="",
            refresh_token="",
            otp_required=True,
            otp_not_enrolled=(otp_requirement == OTPRequirement.NOT_ENROLLED),
            otp_token=_create_otp_token(user, membership),
            must_change_password=auth_result.must_change_password,
        )

    token_data = _build_token_data(user, membership)
    token_data["pwd_change_required"] = auth_result.must_change_password
    return TokenResponse(
        access_token=create_access_token(token_data),
        refresh_token=create_refresh_token({"sub": str(user.id), "org_id": str(membership.org_id)}),
        must_change_password=auth_result.must_change_password,
    )


@router.post("/login", response_model=TokenResponse, summary="Login — JSON body (get JWT tokens)")
async def login_json(
    request: Request,
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Authenticate with a JSON body.

    Supports optional *org_id* to select the target organization — required when
    the user is an external-auth user not yet provisioned in the local DB.
    If OTP is enabled, returns ``otp_required=true`` and a short-lived ``otp_token``.
    """
    result = await db.execute(
        select(GSageUser).where(GSageUser.email == body.email)
    )
    existing_user = result.scalar_one_or_none()

    org = await _resolve_org_for_login(body.org_id, existing_user, db)
    try:
        auth_result, user, membership = await _run_auth_chain(
            body.email, body.password, org, existing_user, db
        )
    except HTTPException:
        await _guardiankey_check(request, body.email, login_failed=True)
        raise

    await _guardiankey_check(request, body.email, login_failed=False)

    otp_requirement = await resolve_otp_requirement(
        org, user, _client_ip(request), request.headers.get("X-Device-Token"), db
    )

    if otp_requirement in (OTPRequirement.REQUIRED, OTPRequirement.NOT_ENROLLED):
        return TokenResponse(
            access_token="",
            refresh_token="",
            otp_required=True,
            otp_not_enrolled=(otp_requirement == OTPRequirement.NOT_ENROLLED),
            otp_token=_create_otp_token(user, membership),
            must_change_password=auth_result.must_change_password,
        )

    token_data = _build_token_data(user, membership)
    token_data["pwd_change_required"] = auth_result.must_change_password
    return TokenResponse(
        access_token=create_access_token(token_data),
        refresh_token=create_refresh_token({"sub": str(user.id), "org_id": str(membership.org_id)}),
        must_change_password=auth_result.must_change_password,
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED,
             summary="Register — create user + first org")
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Create a new user and their first organization.

    Disabled when ``ALLOW_SELF_REGISTER=false`` (default).
    """
    if not get_settings().allow_self_register:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Self-registration is disabled. Contact an administrator.",
        )
    # Check for duplicate email
    existing = await db.execute(
        select(GSageUser).where(GSageUser.email == body.email)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    # Derive slug if not provided
    slug = body.org_slug or _slug_from_name(body.org_name)

    # Check for duplicate slug
    existing_org = await db.execute(
        select(GSageOrganization).where(GSageOrganization.slug == slug)
    )
    if existing_org.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Organization slug '{slug}' is already taken",
        )

    # Create user
    user = GSageUser(
        email=body.email,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
    )
    db.add(user)
    await db.flush()  # Generate user.id before using it in FK

    # Create organization
    org = GSageOrganization(
        name=body.org_name,
        slug=slug,
    )
    db.add(org)
    await db.flush()  # Generate org.id

    # Create owner membership
    membership = GSageUserOrganization(
        user_id=user.id,
        org_id=org.id,
        role="owner",
    )
    db.add(membership)
    await db.commit()
    await db.refresh(membership)

    # Enqueue KB seeding for the new org (best-effort — never blocks registration)
    try:
        from src.backend_api.app.tasks.ingest import load_default_knowledge_task
        cast(Any, load_default_knowledge_task).apply_async(
            kwargs={"org_id": str(org.id)},
            queue="knowledge",
        )
    except Exception as _exc:  # noqa: BLE001
        import logging as _log
        _log.getLogger(__name__).warning("Could not enqueue KB seeding: %s", _exc)

    token_data = _build_token_data(user, membership)
    return TokenResponse(
        access_token=create_access_token(token_data),
        refresh_token=create_refresh_token({"sub": str(user.id), "org_id": str(org.id)}),
    )


@router.post("/refresh", response_model=TokenResponse, summary="Refresh access token")
async def refresh_token(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Issue a new access + refresh token pair using a valid refresh token."""

    # Parse as RefreshRequest if body is a string (backwards compat) or object
    if isinstance(body, str):
        refresh_token_str = body
    else:
        refresh_token_str = body.refresh_token

    try:
        payload = decode_token(refresh_token_str)
        if payload.get("type") != "refresh":
            raise ValueError("Not a refresh token")
        user_id_str: str = payload["sub"]
        org_id_str: str | None = payload.get("org_id")
    except (ValueError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    result = await db.execute(
        select(GSageUser).where(GSageUser.id == uuid.UUID(user_id_str))
    )
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    org_id = uuid.UUID(org_id_str) if org_id_str else None
    membership = await _get_membership_for_login(user, org_id, db)
    token_data = _build_token_data(user, membership)
    return TokenResponse(
        access_token=create_access_token(token_data),
        refresh_token=create_refresh_token({"sub": str(user.id), "org_id": str(membership.org_id)}),
    )


async def _build_me_response(user: GSageUser, db: AsyncSession) -> MeResponse:
    """Build MeResponse including org + dept memberships."""
    from sqlalchemy import and_

    org_result = await db.execute(
        select(GSageUserOrganization)
        .where(
            GSageUserOrganization.user_id == user.id,
            GSageUserOrganization.is_active == True,  # noqa: E712
        )
        .options(selectinload(GSageUserOrganization.organization))
    )
    org_memberships = org_result.scalars().all()

    # Load dept memberships in one query, joined with department for name/slug
    dept_result = await db.execute(
        select(GSageUserDepartment)
        .join(GSageDepartment, GSageUserDepartment.dept_id == GSageDepartment.id)
        .where(
            GSageUserDepartment.user_id == user.id,
            GSageUserDepartment.is_active == True,  # noqa: E712
            GSageDepartment.is_active == True,  # noqa: E712
        )
        .options(selectinload(GSageUserDepartment.department))
    )
    dept_memberships = dept_result.scalars().all()

    # Group dept memberships by org_id for fast lookup
    from collections import defaultdict
    depts_by_org: dict = defaultdict(list)
    for dm in dept_memberships:
        depts_by_org[dm.department.org_id].append(
            DepartmentMembershipOut(
                dept_id=dm.dept_id,
                dept_name=dm.department.name,
                dept_slug=dm.department.slug,
                role=dm.role,
                is_active=dm.is_active,
            )
        )

    return MeResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_active=user.is_active,
        created_at=user.created_at,
        default_dept_id=user.default_dept_id,
        memberships=[
            OrgMembershipOut(
                org_id=m.org_id,
                org_name=m.organization.name,
                org_slug=m.organization.slug,
                role=m.role,
                is_active=m.is_active,
                permissions=permissions_for_role(m.role),
                departments=depts_by_org.get(m.org_id, []),
            )
            for m in org_memberships
        ],
    )


@router.get("/me", response_model=MeResponse, summary="Current user profile")
async def get_me(
    user: GSageUser = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    """Return the authenticated user's profile and all organization memberships."""
    return await _build_me_response(user, db)


@router.patch("/me", response_model=MeResponse, summary="Update current user profile")
async def update_me(
    body: UpdateProfileRequest,
    user: GSageUser = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    """Update the authenticated user's editable profile fields.

    Editable fields: ``full_name``, ``default_dept_id``. Pass ``None`` (or omit
    the field) to leave it unchanged. To clear ``default_dept_id`` send the
    JSON value ``null`` together with ``model_fields_set`` containing the key
    (i.e. include ``"default_dept_id": null`` in the request body).
    """
    fields_set = body.model_fields_set

    if "full_name" in fields_set and body.full_name is not None:
        user.full_name = body.full_name

    if "default_dept_id" in fields_set:
        new_dept_id = body.default_dept_id
        if new_dept_id is None:
            user.default_dept_id = None
        else:
            # Ensure the user is an active member of the chosen department
            # and that the department itself is active.
            membership_check = await db.execute(
                select(GSageUserDepartment)
                .join(GSageDepartment, GSageUserDepartment.dept_id == GSageDepartment.id)
                .where(
                    GSageUserDepartment.user_id == user.id,
                    GSageUserDepartment.dept_id == new_dept_id,
                    GSageUserDepartment.is_active == True,  # noqa: E712
                    GSageDepartment.is_active == True,  # noqa: E712
                )
            )
            if membership_check.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="default_dept_id must reference an active department the user belongs to",
                )
            user.default_dept_id = new_dept_id

    await db.commit()
    await db.refresh(user)
    return await _build_me_response(user, db)


@router.post("/me/change-password", status_code=status.HTTP_204_NO_CONTENT,
             summary="Change current user password")
async def change_password(
    body: ChangePasswordRequest,
    user: GSageUser = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Change the authenticated user's password.

    Validates the current password before setting the new one.
    Only available for users with a local (password-based) account.
    """
    if not user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password change is not available for external-auth accounts",
        )

    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    user.password_hash = hash_password(body.new_password)
    await db.commit()


# ---------------------------------------------------------------------------
# OTP — two-step verification (called after login returns otp_required=true)
# ---------------------------------------------------------------------------


@router.post("/otp/verify", response_model=TokenResponse, summary="Complete OTP verification step")
async def otp_verify(
    request: Request,
    body: OTPVerifyRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Complete login after receiving `otp_required=true`.

    Accepts the short-lived `otp_token` and the 6-digit TOTP code (or a backup
    code prefixed with ``BACKUP:``).  When ``remember_device=true``, returns a
    ``device_token`` that the client should store and send as ``X-Device-Token``
    on future logins to skip OTP.
    """
    user, membership = await _verify_otp_token(body.otp_token, db)

    code = body.code.strip()
    backup_used = False

    if user.otp_enabled and user.otp_secret:
        # Try TOTP first
        if not OTPService.verify_totp(user.otp_secret, code):
            # Try backup code
            backup_codes = user.otp_backup_codes or []
            matched, remaining = OTPService.verify_backup_code(code, backup_codes)
            if not matched:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid OTP code",
                )
            user.otp_backup_codes = remaining
            await db.commit()
            backup_used = True
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP is not configured for this account",
        )

    # Issue device token if requested (only after successful OTP)
    device_token_out: Optional[str] = None
    if body.remember_device and not backup_used:
        raw_token = OTPService.generate_device_token()
        device_hash = OTPService.hash_device_token(raw_token)

        # Resolve TTL from org config
        result = await db.execute(
            select(GSageOrganization).where(GSageOrganization.id == membership.org_id)
        )
        org = result.scalar_one_or_none()
        remember_days = 30
        if org:
            remember_days = org.auth_config.get("otp", {}).get("remember_device_days", 30)

        expires_at = datetime.now(timezone.utc) + timedelta(days=remember_days)
        trusted = GSageTrustedDevice(
            user_id=user.id,
            device_hash=device_hash,
            user_agent=body.user_agent or request.headers.get("User-Agent", "")[:500],
            ip_address=_client_ip(request),
            expires_at=expires_at,
        )
        db.add(trusted)
        await db.commit()
        device_token_out = raw_token

    token_data = _build_token_data(user, membership)
    return TokenResponse(
        access_token=create_access_token(token_data),
        refresh_token=create_refresh_token({"sub": str(user.id), "org_id": str(membership.org_id)}),
        device_token=device_token_out,
    )


# ---------------------------------------------------------------------------
# OTP — enrollment (requires valid access token)
# ---------------------------------------------------------------------------


@router.post("/otp/setup", response_model=OTPSetupResponse, summary="Start OTP enrollment")
async def otp_setup(
    user: GSageUser = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> OTPSetupResponse:
    """Generate a new TOTP secret and QR code for the authenticated user.

    Stores the secret (unconfirmed) and returns the provisioning data.
    The user must call ``POST /auth/otp/confirm`` with a valid code to activate.
    """
    from sqlalchemy import select as _select
    result = await db.execute(
        _select(GSageUserOrganization)
        .where(
            GSageUserOrganization.user_id == user.id,
            GSageUserOrganization.is_active == True,  # noqa: E712
        )
        .limit(1)
    )
    membership = result.scalar_one_or_none()
    issuer = "gSage AI"
    if membership:
        org_result = await db.execute(
            _select(GSageOrganization).where(GSageOrganization.id == membership.org_id)
        )
        org = org_result.scalar_one_or_none()
        if org:
            issuer = org.auth_config.get("otp", {}).get("issuer_name", issuer)

    secret = OTPService.generate_secret()
    user.otp_secret = secret
    user.otp_enabled = False  # not confirmed yet
    user.otp_confirmed_at = None
    await db.commit()

    uri = OTPService.get_provisioning_uri(secret, user.email, issuer)
    qr = OTPService.generate_qr_base64(uri)
    return OTPSetupResponse(secret=secret, provisioning_uri=uri, qr_code=qr)


@router.post("/otp/confirm", response_model=OTPConfirmResponse, summary="Confirm OTP enrollment")
async def otp_confirm(
    body: OTPConfirmRequest,
    user: GSageUser = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> OTPConfirmResponse:
    """Confirm OTP enrollment by submitting the first valid TOTP code.

    Returns the one-time backup codes — show them to the user once and store
    only the bcrypt hashes.
    """
    if not user.otp_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP setup not initiated. Call POST /auth/otp/setup first.",
        )

    if not OTPService.verify_totp(user.otp_secret, body.code.strip()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OTP code",
        )

    plaintext_codes, hashed_codes = OTPService.generate_backup_codes(10)
    user.otp_enabled = True
    user.otp_confirmed_at = datetime.now(timezone.utc)
    user.otp_backup_codes = hashed_codes
    await db.commit()

    return OTPConfirmResponse(backup_codes=plaintext_codes)


@router.delete("/otp", status_code=status.HTTP_204_NO_CONTENT, summary="Disable OTP for current user")
async def otp_disable(
    body: OTPDisableRequest,
    user: GSageUser = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Disable OTP for the current user.

    For local accounts, requires current password or a valid OTP code.
    Trusted devices for this user are also cleared.
    """
    if not user.otp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP is not enabled for this account",
        )

    # Verify identity: password OR otp code
    verified = False
    if body.password and user.password_hash:
        verified = verify_password(body.password, user.password_hash)
    if not verified and body.code and user.otp_secret:
        verified = OTPService.verify_totp(user.otp_secret, body.code.strip())

    if not verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password or OTP code required to disable OTP",
        )

    user.otp_secret = None
    user.otp_enabled = False
    user.otp_confirmed_at = None
    user.otp_backup_codes = None

    # Remove all trusted devices
    from sqlalchemy import delete as _delete
    await db.execute(
        _delete(GSageTrustedDevice).where(GSageTrustedDevice.user_id == user.id)
    )
    await db.commit()


@router.post("/otp/backup-codes/regenerate", response_model=OTPConfirmResponse,
             summary="Regenerate backup codes")
async def otp_regenerate_backup_codes(
    body: OTPDisableRequest,
    user: GSageUser = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> OTPConfirmResponse:
    """Regenerate backup codes (invalidates the current ones).

    Requires current password or a valid OTP code for confirmation.
    """
    if not user.otp_enabled or not user.otp_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP is not enabled for this account",
        )

    verified = False
    if body.password and user.password_hash:
        verified = verify_password(body.password, user.password_hash)
    if not verified and body.code and user.otp_secret:
        verified = OTPService.verify_totp(user.otp_secret, body.code.strip())

    if not verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password or OTP code required to regenerate backup codes",
        )

    plaintext_codes, hashed_codes = OTPService.generate_backup_codes(10)
    user.otp_backup_codes = hashed_codes
    await db.commit()

    return OTPConfirmResponse(backup_codes=plaintext_codes)


@router.get("/otp/status", response_model=OTPStatusResponse, summary="OTP status for current user")
async def otp_status(
    user: GSageUser = Depends(get_current_active_user),
) -> OTPStatusResponse:
    """Return the OTP enrollment status for the authenticated user."""
    backup_count: Optional[int] = None
    if user.otp_enabled and user.otp_backup_codes is not None:
        backup_count = len(user.otp_backup_codes)
    return OTPStatusResponse(
        otp_enabled=user.otp_enabled,
        otp_confirmed_at=user.otp_confirmed_at,
        backup_codes_count=backup_count,
    )
