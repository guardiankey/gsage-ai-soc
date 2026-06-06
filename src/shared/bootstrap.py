"""gSage AI — Admin bootstrap / seed.

Creates the initial organization, admin user, and a wildcard API key on
first startup. Called from the FastAPI lifespan and from the
``scripts/get_admin.py`` helper script.

Design
------
* Idempotent: if a user with ``admin_email`` already exists the function
  returns ``None`` immediately — no duplicate rows, no errors.
* The raw API key is returned **once** and never stored (only its
  SHA-256 hash is persisted).  The caller is responsible for logging it
  clearly.
* ``scoped_permissions = ["*"]`` is intentional for the bootstrap key:
  the wildcard is handled by ``filter_permissions_by_scope()`` and means
  "inherit all of the user's effective permissions".

Usage::

    from src.shared.bootstrap import ensure_admin
    from src.shared.database import _get_session_maker

    async with _get_session_maker()() as session:
        raw_key = await ensure_admin(session)
        if raw_key:
            print(f"API key: {raw_key}")
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models import (
    GSageAPIKey,
    GSageDepartment,
    GSageGroup,
    GSageOrganization,
    GSagePermission,
    GSageUser,
    GSageUserDepartment,
    GSageUserOrganization,
    gsage_user_groups,
)
from src.shared.security.auth import generate_api_key, hash_password


def _slugify(name: str) -> str:
    """Convert *name* to a URL-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug[:100]


async def ensure_admin(session: AsyncSession) -> Optional[str]:
    """Create the bootstrap admin org + user + API key if they don't exist.

    Reads credentials from ``Settings`` (environment variables
    ``ADMIN_EMAIL``, ``ADMIN_PASSWORD``, ``ADMIN_ORG_NAME``).

    Returns
    -------
    str
        The raw API key (``gk_live_...``) — returned **only on first
        creation**.  Log it immediately; it cannot be recovered later.
    None
        Bootstrap is disabled (``ADMIN_EMAIL`` is empty) or the admin user
        already exists.
    """
    from src.shared.config.settings import get_settings
    settings = get_settings()

    if not settings.admin_email:
        return None

    # ── Idempotency check ──────────────────────────────────────────────────
    result = await session.execute(
        select(GSageUser).where(GSageUser.email == settings.admin_email)
    )
    if result.scalar_one_or_none() is not None:
        return None  # already seeded

    # ── Organisation ───────────────────────────────────────────────────────
    org_name = settings.admin_org_name or "Default Organization"
    slug = _slugify(org_name)

    # Ensure slug uniqueness (append suffix if needed)
    existing_slug = await session.execute(
        select(GSageOrganization).where(GSageOrganization.slug == slug)
    )
    if existing_slug.scalar_one_or_none() is not None:
        slug = f"{slug}-admin"

    # Resolve maker/reviewer model from llm_provider setting
    provider = settings.llm_provider.lower()
    if provider == "openai":
        maker_model = settings.openai_maker_model
    elif provider == "deepseek":
        maker_model = settings.deepseek_maker_model
    elif provider == "gemini":
        maker_model = settings.gemini_maker_model
    elif provider == "anthropic":
        maker_model = settings.anthropic_maker_model
    elif provider == "vllm":
        maker_model = settings.vllm_maker_model
    else:
        maker_model = settings.ollama_maker_model
    # reviewer defaults to same as maker (no separate reviewer setting)
    reviewer_model = maker_model

    org = GSageOrganization(
        name=org_name,
        slug=slug,
        llm_provider=provider,
        default_maker_model=maker_model,
        default_reviewer_model=reviewer_model,
    )

    session.add(org)
    await session.flush()  # populate org.id

    # ── User ───────────────────────────────────────────────────────────────
    user = GSageUser(
        email=settings.admin_email,
        password_hash=hash_password(settings.admin_password),
        full_name="Admin",
        is_active=True,
    )
    session.add(user)
    await session.flush()  # populate user.id

    # ── Membership (owner) ─────────────────────────────────────────────────
    membership = GSageUserOrganization(
        user_id=user.id,
        org_id=org.id,
        role="owner",
        is_active=True,
    )
    session.add(membership)
    await session.flush()

    # ── Default Department ─────────────────────────────────────────────────
    default_dept = GSageDepartment(
        org_id=org.id,
        name="Default",
        slug="default",
        is_default=True,
        is_active=True,
    )
    session.add(default_dept)
    await session.flush()  # populate default_dept.id

    # Add admin user as dept admin
    dept_membership = GSageUserDepartment(
        user_id=user.id,
        dept_id=default_dept.id,
        role="admin",
        is_active=True,
    )
    session.add(dept_membership)
    await session.flush()

    # ── Admin Group + Wildcard Permission ──────────────────────────────────
    # Create (or reuse) the wildcard permission tag="*" and an
    # "Administrators" group so the admin user has access to ALL tools.
    wildcard_result = await session.execute(
        select(GSagePermission).where(GSagePermission.tag == "*")
    )
    wildcard_perm = wildcard_result.scalar_one_or_none()
    if wildcard_perm is None:
        wildcard_perm = GSagePermission(
            tag="*",
            description="Wildcard — grants access to all tools",
            category="admin",
        )
        session.add(wildcard_perm)
        await session.flush()

    admin_group = GSageGroup(
        org_id=org.id,
        name="Administrators",
        description="Full access to all tools and operations",
    )
    admin_group.permissions.append(wildcard_perm)
    session.add(admin_group)
    await session.flush()

    # Link admin user to the group
    await session.execute(
        gsage_user_groups.insert().values(
            user_id=user.id,
            group_id=admin_group.id,
        )
    )
    await session.flush()

    # ── API Key ────────────────────────────────────────────────────────────
    # scoped_permissions=["*"] → wildcard: inherits all user permissions.
    # We bypass validate_permission_tags() intentionally — the wildcard is
    # a synthetic token handled by filter_permissions_by_scope(), not a real
    # tag stored in gsage_permissions.
    raw_key, key_hash, key_prefix = generate_api_key("live")
    expires_at = datetime.now(timezone.utc) + timedelta(days=365)

    api_key = GSageAPIKey(
        org_id=org.id,
        user_id=user.id,
        name="Bootstrap Admin Key",
        key_hash=key_hash,
        key_prefix=key_prefix,
        environment="live",
        scoped_permissions=["*"],
        expires_at=expires_at,
        rate_limit_per_minute=1000,
        is_active=True,
        created_at=datetime.now(timezone.utc),
        last_used_at=None,
        revoked_at=None,
    )
    session.add(api_key)
    await session.commit()

    # Enqueue KB seeding for the new org (best-effort — never blocks bootstrap)
    try:
        from src.backend_api.app.tasks.ingest import load_default_knowledge_task
        cast(Any, load_default_knowledge_task).apply_async(
            kwargs={"org_id": str(org.id)},
            queue="knowledge",
        )
    except Exception as _exc:  # noqa: BLE001
        import logging as _log
        _log.getLogger(__name__).warning("Could not enqueue KB seeding: %s", _exc)

    return raw_key


async def rotate_admin_key(session: AsyncSession) -> Optional[tuple[str, Optional[str]]]:
    """Revoke all active admin API keys and issue a fresh one.

    Returns
    -------
    (raw_key, org_id)
        The new raw API key and the admin org UUID string.
    None
        Admin user not found or bootstrap is disabled.
    """
    from src.shared.config.settings import get_settings
    settings = get_settings()

    if not settings.admin_email:
        return None

    result = await session.execute(
        select(GSageUser).where(GSageUser.email == settings.admin_email)
    )
    user = result.scalar_one_or_none()
    if user is None:
        return None

    # Find org via ownership membership
    mem_result = await session.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == user.id,
            GSageUserOrganization.role == "owner",
        )
    )
    membership = mem_result.scalars().first()
    org_id = membership.org_id if membership else None

    # Revoke all active keys for this user
    key_result = await session.execute(
        select(GSageAPIKey).where(
            GSageAPIKey.user_id == user.id,
            GSageAPIKey.is_active.is_(True),
        )
    )
    for key in key_result.scalars().all():
        key.is_active = False
        key.revoked_at = datetime.now(timezone.utc)

    # Create replacement key
    raw_key, key_hash, key_prefix = generate_api_key("live")
    expires_at = datetime.now(timezone.utc) + timedelta(days=365)
    new_key = GSageAPIKey(
        org_id=org_id,
        user_id=user.id,
        name="Bootstrap Admin Key (rotated)",
        key_hash=key_hash,
        key_prefix=key_prefix,
        environment="live",
        scoped_permissions=["*"],
        expires_at=expires_at,
        rate_limit_per_minute=1000,
        is_active=True,
        created_at=datetime.now(timezone.utc),
        last_used_at=None,
        revoked_at=None,
    )
    session.add(new_key)
    await session.commit()

    return raw_key, str(org_id) if org_id else None


async def get_admin_info(session: AsyncSession) -> Optional[dict]:
    """Return basic admin user + org info for display purposes.

    Used by ``scripts/get_admin.py`` when the raw key is no longer in
    the logs but the admin user already exists.

    Returns ``None`` if no admin user exists.
    """
    from src.shared.config.settings import get_settings
    settings = get_settings()

    if not settings.admin_email:
        return None

    result = await session.execute(
        select(GSageUser).where(GSageUser.email == settings.admin_email)
    )
    user = result.scalar_one_or_none()
    if user is None:
        return None

    # Find org via membership
    mem_result = await session.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == user.id,
            GSageUserOrganization.role == "owner",
        )
    )
    membership = mem_result.scalars().first()

    org = None
    if membership:
        org_result = await session.execute(
            select(GSageOrganization).where(
                GSageOrganization.id == membership.org_id
            )
        )
        org = org_result.scalar_one_or_none()

    # Fetch API key prefix (raw key is gone — only prefix survives)
    key_result = await session.execute(
        select(GSageAPIKey).where(
            GSageAPIKey.user_id == user.id,
            GSageAPIKey.is_active.is_(True),
        )
    )
    keys = key_result.scalars().all()

    # Fetch default department for the org
    dept_id: Optional[str] = None
    if org:
        dept_result = await session.execute(
            select(GSageDepartment).where(
                GSageDepartment.org_id == org.id,
                GSageDepartment.is_default.is_(True),
            )
        )
        dept = dept_result.scalar_one_or_none()
        if dept is not None:
            dept_id = str(dept.id)

    return {
        "user_id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "org_id": str(org.id) if org else None,
        "org_name": org.name if org else None,
        "org_slug": org.slug if org else None,
        "dept_id": dept_id,
        "role": membership.role if membership else None,
        "api_keys": [
            {
                "id": str(k.id),
                "name": k.name,
                "prefix": k.key_prefix,
                "environment": k.environment,
                "expires_at": k.expires_at.isoformat(),
                "scoped_permissions": k.scoped_permissions,
            }
            for k in keys
        ],
    }


async def reset_admin_password(session: AsyncSession, new_password: str) -> bool:
    """Update the bootstrap admin user's password hash.

    Parameters
    ----------
    session:
        Async SQLAlchemy session.
    new_password:
        Plain-text password to hash and store.

    Returns
    -------
    bool
        ``True`` on success, ``False`` if the admin user was not found or
        bootstrap is disabled.
    """
    from src.shared.config.settings import get_settings
    settings = get_settings()

    if not settings.admin_email:
        return False

    result = await session.execute(
        select(GSageUser).where(GSageUser.email == settings.admin_email)
    )
    user = result.scalar_one_or_none()
    if user is None:
        return False

    user.password_hash = hash_password(new_password)
    await session.commit()
    return True