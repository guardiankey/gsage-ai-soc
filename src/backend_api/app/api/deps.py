"""gSage AI — FastAPI dependencies (auth, db, org membership)."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.core.tenant import TenantContext, permissions_for_role
from src.shared.database import get_db
from src.shared.models.api_key import GSageAPIKey
from src.shared.models.department import GSageDepartment
from src.shared.models.user import GSageUser
from src.shared.models.user_department import GSageUserDepartment
from src.shared.models.user_organization import GSageUserOrganization
from src.shared.security.auth import decode_token, hash_api_key

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)
_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


# ---------------------------------------------------------------------------
# Basic user auth (backwards-compat — used by /me and other user-scoped routes)
# ---------------------------------------------------------------------------


async def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> GSageUser:
    """Decode JWT and return the authenticated user.

    Also accepts API key tokens (``gk_live_*`` / ``gk_test_*``) — in that
    case the key must be bound to a specific user (``user_id`` set).
    """
    if not token:
        raise _UNAUTHORIZED

    # --- API key path -------------------------------------------------------
    if token.startswith("gk_live_") or token.startswith("gk_test_"):
        key_hash = hash_api_key(token)
        key_result = await db.execute(
            select(GSageAPIKey).where(
                GSageAPIKey.key_hash == key_hash,
                GSageAPIKey.is_active == True,  # noqa: E712
            )
        )
        db_key = key_result.scalar_one_or_none()
        if db_key is None:
            raise _INVALID_CREDENTIALS
        if db_key.user_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This API key is not bound to a user account. Use a personal API key or login with email/password.",
            )
        user_result = await db.execute(
            select(GSageUser).where(GSageUser.id == db_key.user_id)
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            raise _INVALID_CREDENTIALS
        return user

    # --- JWT path -----------------------------------------------------------
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise _INVALID_CREDENTIALS
        user_id_str: Optional[str] = payload.get("sub")
        if not user_id_str:
            raise _INVALID_CREDENTIALS
    except ValueError:
        raise _INVALID_CREDENTIALS

    result = await db.execute(
        select(GSageUser).where(GSageUser.id == uuid.UUID(user_id_str))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise _INVALID_CREDENTIALS
    return user


async def get_current_active_user(
    user: GSageUser = Depends(get_current_user),
) -> GSageUser:
    """Ensure the authenticated user is active."""
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    return user


async def get_org_membership(
    org_id: uuid.UUID,
    user: GSageUser = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> GSageUserOrganization:
    """Return the membership record for the current user in the given org.

    Raises 403 if the user is not a member or the membership is inactive.
    """
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
            detail="Access denied to this organization",
        )
    return membership


async def require_org_admin(
    membership: GSageUserOrganization = Depends(get_org_membership),
) -> GSageUserOrganization:
    """Ensure the current user has admin or owner role in the given org."""
    if membership.role not in ("owner", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return membership


async def get_api_key_context(
    api_key: Optional[str] = Security(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> GSageAPIKey:
    """Validate X-API-Key header and return the active key record."""
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
        )
    key_hash = hash_api_key(api_key)
    result = await db.execute(
        select(GSageAPIKey).where(
            GSageAPIKey.key_hash == key_hash,
            GSageAPIKey.is_active == True,  # noqa: E712
        )
    )
    db_key = result.scalar_one_or_none()
    if db_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
        )
    return db_key


# ---------------------------------------------------------------------------
# Helper: resolve department context from X-Department-Id header
# ---------------------------------------------------------------------------


async def _resolve_dept_from_header(
    request: Request,
    ctx: TenantContext,
    db: AsyncSession,
) -> TenantContext:
    """Optionally extend TenantContext with department context from X-Department-Id header.

    If the header is absent, malformed, the department does not belong to the org,
    or the user is not a member, returns ctx unchanged — never raises.
    Department context is optional; absence is not an authentication error.
    """
    dept_id_raw = request.headers.get("X-Department-Id")
    if not dept_id_raw:
        return ctx
    try:
        dept_id = uuid.UUID(dept_id_raw)
    except ValueError:
        return ctx  # Malformed UUID — ignore silently

    try:
        dept_result = await db.execute(
            select(GSageDepartment).where(
                and_(
                    GSageDepartment.id == dept_id,
                    GSageDepartment.org_id == ctx.org_id,
                    GSageDepartment.is_active.is_(True),
                )
            )
        )
        dept = dept_result.scalar_one_or_none()
        if dept is None:
            return ctx  # Department not found in this org — ignore silently

        # Org admins/owners bypass membership check
        dept_role: Optional[str] = None
        if ctx.org_role not in ("owner", "admin"):
            mem_result = await db.execute(
                select(GSageUserDepartment).where(
                    and_(
                        GSageUserDepartment.user_id == ctx.user_id,
                        GSageUserDepartment.dept_id == dept_id,
                        GSageUserDepartment.is_active.is_(True),
                    )
                )
            )
            membership = mem_result.scalar_one_or_none()
            if membership is None:
                return ctx  # No department membership — ignore silently
            dept_role = membership.role

        return dataclasses.replace(ctx, dept_id=dept_id, dept_role=dept_role)
    except Exception:
        return ctx  # DB error — degrade silently, never block the request


# ---------------------------------------------------------------------------
# TenantContext — primary dependency for org-scoped routes
# ---------------------------------------------------------------------------


async def get_tenant_context(
    request: Request,
    org_id: uuid.UUID,
    token: Optional[str] = Depends(oauth2_scheme),
    raw_auth: Optional[str] = Security(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> TenantContext:
    """Resolve a ``TenantContext`` for the current request.

    Supports two authentication methods tried in order:

    1. **JWT Bearer** — ``Authorization: Bearer <jwt>``  
       Validates the token, checks that the embedded ``org_id`` matches the
       route path parameter, and verifies the user is an active org member.

    2. **API Key** — ``Authorization: Bearer gk_live_...`` or ``gk_test_...``  
       Looks up the hashed key in the DB, validates it belongs to *org_id*,
       and builds a TenantContext from the key's scoped permissions.

    Raises:
        HTTP 401: No credentials provided or credentials are invalid.
        HTTP 403: Credentials valid but org access denied.
    """
    # ---- Determine credential type ----------------------------------------
    bearer_token: Optional[str] = token  # set by OAuth2PasswordBearer

    # If raw_auth header starts with "gk_" it's an API key even when passed
    # as "Authorization: Bearer gk_live_..."
    api_key_raw: Optional[str] = None
    if raw_auth:
        # Strip "Bearer " prefix if present
        candidate = raw_auth.removeprefix("Bearer ").strip()
        if candidate.startswith("gk_live_") or candidate.startswith("gk_test_"):
            api_key_raw = candidate

    # ---- Path 1: JWT -------------------------------------------------------
    if bearer_token and not api_key_raw:
        try:
            payload = decode_token(bearer_token)
        except ValueError:
            raise _INVALID_CREDENTIALS

        if payload.get("type") != "access":
            raise _INVALID_CREDENTIALS

        token_org_id_str: Optional[str] = payload.get("org_id")
        user_id_str: Optional[str] = payload.get("sub")
        if not token_org_id_str or not user_id_str:
            raise _INVALID_CREDENTIALS

        # The JWT org_id must match the route's org_id
        try:
            token_org_id = uuid.UUID(token_org_id_str)
        except ValueError:
            raise _INVALID_CREDENTIALS

        if token_org_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token org_id does not match route organization",
            )

        # Verify user still belongs to the org (DB check)
        result = await db.execute(
            select(GSageUserOrganization).where(
                GSageUserOrganization.user_id == uuid.UUID(user_id_str),
                GSageUserOrganization.org_id == org_id,
                GSageUserOrganization.is_active == True,  # noqa: E712
            )
        )
        membership = result.scalar_one_or_none()
        if membership is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not an active member of this organization",
            )

        # Always derive role & permissions from the DB membership so that
        # role changes take effect immediately (JWT claims may be stale).
        permissions: list[str] = permissions_for_role(membership.role)

        ctx = TenantContext(
            user_id=uuid.UUID(user_id_str),
            org_id=org_id,
            org_role=membership.role,
            permissions=permissions,
            email=payload.get("email"),
        )
        ctx = await _resolve_dept_from_header(request, ctx, db)
        request.state.tenant = ctx
        return ctx

    # ---- Path 2: API Key ---------------------------------------------------
    if api_key_raw:
        key_hash = hash_api_key(api_key_raw)
        result = await db.execute(
            select(GSageAPIKey).where(
                GSageAPIKey.key_hash == key_hash,
                GSageAPIKey.org_id == org_id,
                GSageAPIKey.is_active == True,  # noqa: E712
                GSageAPIKey.revoked_at.is_(None),
            )
        )
        db_key = result.scalar_one_or_none()
        if db_key is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid, revoked, or org-mismatched API key",
            )

        # Determine user_id and permissions
        user_id: uuid.UUID
        role: str
        permissions_list: list[str]

        if db_key.user_id is not None:
            # Personal key — inherit the user's org permissions
            mem_result = await db.execute(
                select(GSageUserOrganization).where(
                    GSageUserOrganization.user_id == db_key.user_id,
                    GSageUserOrganization.org_id == org_id,
                    GSageUserOrganization.is_active == True,  # noqa: E712
                )
            )
            membership = mem_result.scalar_one_or_none()
            if membership is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="API key owner is no longer a member of this organization",
                )
            user_id = db_key.user_id
            role = membership.role
            permissions_list = permissions_for_role(role)
        else:
            # Org-level key — use explicit scoped_permissions
            # Use a synthetic UUID keyed to the org for org-level keys
            user_id = org_id  # no real user; use org id as sentinel
            role = "apikey"
            permissions_list = list(db_key.scoped_permissions or [])

        # Resolve interface server-side — never from a client-supplied header.
        # Org-level keys default to "api"; personal keys default to "web".
        resolved_interface = db_key.interface or (
            "api" if db_key.user_id is None else "web"
        )

        ctx = TenantContext(
            user_id=user_id,
            org_id=org_id,
            org_role=role,
            permissions=permissions_list,
            rate_limit_per_minute=getattr(db_key, "rate_limit_per_minute", None),
            interface=resolved_interface,
        )
        ctx = await _resolve_dept_from_header(request, ctx, db)
        request.state.tenant = ctx
        return ctx

    # ---- No credentials ---------------------------------------------------
    raise _UNAUTHORIZED


def require_permission(permission: str) -> Callable:
    """Dependency factory that enforces a specific permission on TenantContext.

    Usage::

        @router.post("/run")
        async def run_agent(
            ctx: TenantContext = Depends(get_tenant_context),
            _: None = Depends(require_permission("agents:run")),
        ) -> ...:
            ...

    Or combined::

        async def run_agent(
            ctx: Annotated[TenantContext, Depends(get_tenant_context)],
            _perm: Annotated[None, Depends(require_permission("agents:run"))],
        ) -> ...:
    """

    async def _check(ctx: TenantContext = Depends(get_tenant_context)) -> None:
        ctx.require_permission(permission)

    # Give the inner function a unique name so FastAPI doesn't de-duplicate it
    _check.__name__ = f"require_{permission.replace(':', '_')}"
    return _check


# ---------------------------------------------------------------------------
# Department context dependency
# ---------------------------------------------------------------------------


async def get_department_context(
    request: Request,
    dept_id: uuid.UUID,
    ctx: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
) -> TenantContext:
    """Extend TenantContext with department membership info.

    Reads ``dept_id`` from the route path parameter and validates that:
    1. The department exists within the current org.
    2. The user is an active member OR has org-level admin/owner role.

    Returns a new TenantContext with ``dept_id`` and ``dept_role`` populated.
    org admins/owners bypass membership checks (dept_role stays None).
    """
    # Validate department belongs to the org
    dept_result = await db.execute(
        select(GSageDepartment).where(
            and_(
                GSageDepartment.id == dept_id,
                GSageDepartment.org_id == ctx.org_id,
                GSageDepartment.is_active.is_(True),
            )
        )
    )
    dept = dept_result.scalar_one_or_none()
    if dept is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Department not found in this organization",
        )

    # Org admins/owners bypass membership check
    dept_role: Optional[str] = None
    if ctx.org_role not in ("owner", "admin"):
        mem_result = await db.execute(
            select(GSageUserDepartment).where(
                and_(
                    GSageUserDepartment.user_id == ctx.user_id,
                    GSageUserDepartment.dept_id == dept_id,
                    GSageUserDepartment.is_active.is_(True),
                )
            )
        )
        membership = mem_result.scalar_one_or_none()
        if membership is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not a member of this department",
            )
        dept_role = membership.role

    updated_ctx = dataclasses.replace(ctx, dept_id=dept_id, dept_role=dept_role)
    request.state.tenant = updated_ctx
    return updated_ctx

