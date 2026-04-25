"""Admin Console — service functions for Organizations."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def list_orgs(db: AsyncSession) -> list[dict[str, Any]]:
    """Return all organizations ordered by name."""
    from src.shared.models.organization import GSageOrganization  # noqa: PLC0415

    result = await db.execute(
        select(GSageOrganization).order_by(GSageOrganization.name)
    )
    rows = result.scalars().all()
    return [_org_to_dict(o) for o in rows]


async def get_org(db: AsyncSession, org_id: uuid.UUID) -> Optional[dict[str, Any]]:
    """Return a single org dict or None."""
    from src.shared.models.organization import GSageOrganization  # noqa: PLC0415

    result = await db.execute(
        select(GSageOrganization).where(GSageOrganization.id == org_id)
    )
    org = result.scalar_one_or_none()
    return _org_to_dict(org) if org else None


async def get_org_model(db: AsyncSession, org_id: uuid.UUID):
    """Return the raw GSageOrganization model instance or None."""
    from src.shared.models.organization import GSageOrganization  # noqa: PLC0415

    result = await db.execute(
        select(GSageOrganization).where(GSageOrganization.id == org_id)
    )
    return result.scalar_one_or_none()


async def create_org(
    db: AsyncSession,
    name: str,
    slug: str,
    llm_provider: str = "ollama",
    extra_fields: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create a new organization and return its dict."""
    from src.shared.models.organization import GSageOrganization  # noqa: PLC0415

    org = GSageOrganization(
        name=name.strip(),
        slug=slug.strip().lower(),
        is_active=True,
        llm_provider=llm_provider,
    )
    if extra_fields:
        _apply_org_fields(org, extra_fields)
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return _org_to_dict(org)


async def update_org(
    db: AsyncSession,
    org_id: uuid.UUID,
    **fields: Any,
) -> Optional[dict[str, Any]]:
    """Update an org and return updated dict."""
    org = await get_org_model(db, org_id)
    if not org:
        return None
    _apply_org_fields(org, fields)
    await db.commit()
    await db.refresh(org)
    return _org_to_dict(org)


async def update_org_smtp(
    db: AsyncSession,
    org_id: uuid.UUID,
    smtp_dict: dict[str, Any],
) -> None:
    """Store (encrypted) SMTP config for an org."""
    org = await get_org_model(db, org_id)
    if not org:
        raise ValueError(f"Organization {org_id} not found")
    org.smtp_config = smtp_dict
    await db.commit()


async def update_org_auth_config(
    db: AsyncSession,
    org_id: uuid.UUID,
    auth_config: dict[str, Any],
) -> None:
    """Store (encrypted) auth config for an org."""
    org = await get_org_model(db, org_id)
    if not org:
        raise ValueError(f"Organization {org_id} not found")
    org.auth_config = auth_config
    await db.commit()


async def toggle_org_active(
    db: AsyncSession,
    org_id: uuid.UUID,
) -> Optional[dict[str, Any]]:
    """Toggle is_active flag on an org."""
    from src.shared.models.organization import GSageOrganization  # noqa: PLC0415

    result = await db.execute(
        select(GSageOrganization).where(GSageOrganization.id == org_id)
    )
    org = result.scalar_one_or_none()
    if not org:
        return None
    org.is_active = not org.is_active
    await db.commit()
    await db.refresh(org)
    return _org_to_dict(org)


async def org_stats(db: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    """Return aggregate counts for an org."""
    from src.shared.models.user_organization import GSageUserOrganization  # noqa: PLC0415
    from src.shared.models.tenant_session import GSageTenantSession  # noqa: PLC0415
    from src.shared.models.agent_run import GSageAgentRun  # noqa: PLC0415

    user_count = (await db.execute(
        select(func.count()).where(
            GSageUserOrganization.org_id == org_id,
            GSageUserOrganization.is_active.is_(True),
        )
    )).scalar_one()

    session_count = (await db.execute(
        select(func.count()).where(GSageTenantSession.org_id == org_id)
    )).scalar_one()

    run_count = (await db.execute(
        select(func.count()).where(GSageAgentRun.org_id == org_id)
    )).scalar_one()

    return {
        "users": user_count,
        "sessions": session_count,
        "agent_runs": run_count,
    }


def _apply_org_fields(org: Any, fields: dict[str, Any]) -> None:
    """Apply a flat fields dict to a GSageOrganization instance, handling encrypted properties."""
    _skip = {"id", "created_at", "updated_at"}
    _int_fields = {"agent_timeout_seconds", "max_context_tokens", "port"}
    for key, value in fields.items():
        if key in _skip or value is None:
            continue
        if key == "auth_providers" and isinstance(value, str):
            # Convert comma-separated string to list
            parts = [p.strip() for p in value.split(",") if p.strip()]
            org.auth_providers = parts or ["local"]
        elif key in _int_fields:
            try:
                setattr(org, key, int(value))
            except (TypeError, ValueError):
                pass
        elif key == "is_active" and isinstance(value, str):
            setattr(org, key, value.lower() not in ("false", "0", ""))
        else:
            setattr(org, key, value)


def _org_to_dict(org: Any) -> dict[str, Any]:
    return {
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "is_active": org.is_active,
        "llm_provider": org.llm_provider or "",
        "llm_api_key": "***" if org._llm_api_key_encrypted else "",
        "default_maker_model": org.default_maker_model or "",
        "default_reviewer_model": org.default_reviewer_model or "",
        "system_prompt": org.system_prompt or "",
        "agent_timeout_seconds": str(org.agent_timeout_seconds),
        "max_context_tokens": str(org.max_context_tokens),
        "auth_providers": ", ".join(org.auth_providers),
        "smtp_configured": "Yes" if org._smtp_config_encrypted else "No",
        "created_at": org.created_at.isoformat() if org.created_at else "",
    }
