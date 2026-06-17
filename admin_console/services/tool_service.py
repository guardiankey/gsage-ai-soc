"""Admin Console — service functions for Tool Configs and Interface Profiles."""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def _invalidate_tool_config_cache(org_id: uuid.UUID) -> None:
    """Best-effort flush of the MCP server's cached tool config for an org."""
    from src.shared.cache.permissions_cache import get_perm_redis_client  # noqa: PLC0415
    from src.shared.cache.tool_config_cache import (  # noqa: PLC0415
        invalidate_tool_config_cache,
    )

    await invalidate_tool_config_cache(get_perm_redis_client(), org_id)


async def list_tool_configs(
    db: AsyncSession,
    org_id: uuid.UUID,
) -> list[dict[str, Any]]:
    from src.shared.models.tool_config import GSageToolConfig  # noqa: PLC0415

    result = await db.execute(
        select(GSageToolConfig)
        .where(GSageToolConfig.org_id == org_id)
        .order_by(GSageToolConfig.tool_name, GSageToolConfig.profile_id)
    )
    return [_tool_config_to_dict(tc) for tc in result.scalars().all()]


async def get_tool_config(
    db: AsyncSession,
    config_id: uuid.UUID,
) -> Optional[dict[str, Any]]:
    from src.shared.models.tool_config import GSageToolConfig  # noqa: PLC0415

    result = await db.execute(
        select(GSageToolConfig).where(GSageToolConfig.id == config_id)
    )
    tc = result.scalar_one_or_none()
    return _tool_config_to_dict(tc) if tc else None


async def create_tool_config(
    db: AsyncSession,
    org_id: uuid.UUID,
    tool_name: str,
    profile_id: str,
    config: dict,
    description: str = "",
) -> dict[str, Any]:
    from src.shared.models.tool_config import GSageToolConfig  # noqa: PLC0415
    from src.shared.security.encryption import get_encryption  # noqa: PLC0415

    tc = GSageToolConfig(
        org_id=org_id,
        tool_name=tool_name.strip(),
        profile_id=profile_id.strip(),
        description=description.strip() or None,
        _config_encrypted=get_encryption().encrypt(json.dumps(config)),
    )
    db.add(tc)
    await db.commit()
    await db.refresh(tc)
    await _invalidate_tool_config_cache(org_id)
    return _tool_config_to_dict(tc)


async def update_tool_config(
    db: AsyncSession,
    config_id: uuid.UUID,
    config: dict,
    description: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    from src.shared.models.tool_config import GSageToolConfig  # noqa: PLC0415
    from src.shared.security.encryption import get_encryption  # noqa: PLC0415

    values: dict = {"_config_encrypted": get_encryption().encrypt(json.dumps(config))}
    if description is not None:
        values["description"] = description
    await db.execute(
        update(GSageToolConfig)
        .where(GSageToolConfig.id == config_id)
        .values(**values)
    )
    await db.commit()
    updated = await get_tool_config(db, config_id)
    if updated is not None and updated.get("org_id"):
        await _invalidate_tool_config_cache(uuid.UUID(str(updated["org_id"])))
    return updated


async def delete_tool_config(db: AsyncSession, config_id: uuid.UUID) -> bool:
    from src.shared.models.tool_config import GSageToolConfig  # noqa: PLC0415

    # Capture org_id before delete for cache invalidation.
    existing = await get_tool_config(db, config_id)
    await db.execute(
        delete(GSageToolConfig).where(GSageToolConfig.id == config_id)
    )
    await db.commit()
    if existing is not None and existing.get("org_id"):
        await _invalidate_tool_config_cache(uuid.UUID(str(existing["org_id"])))
    return True


async def list_interface_profiles(
    db: AsyncSession,
    org_id: uuid.UUID,
) -> list[dict[str, Any]]:
    from src.shared.models.interface_profile import GSageInterfaceProfile  # noqa: PLC0415

    result = await db.execute(
        select(GSageInterfaceProfile)
        .where(GSageInterfaceProfile.org_id == org_id)
        .order_by(GSageInterfaceProfile.interface)
    )
    return [_profile_to_dict(p) for p in result.scalars().all()]


async def create_interface_profile(
    db: AsyncSession,
    org_id: uuid.UUID,
    interface: str,
    mode: str = "allowlist",
    description: str = "",
    *,
    dept_id: Optional[uuid.UUID] = None,
    system_prompt: Optional[str] = None,
    interface_config: Optional[dict] = None,
    preferences: Optional[dict] = None,
) -> dict[str, Any]:
    from src.shared.models.interface_profile import GSageInterfaceProfile  # noqa: PLC0415

    profile = GSageInterfaceProfile(
        org_id=org_id,
        dept_id=dept_id,
        interface=interface.strip(),
        mode=mode,
        description=description.strip() or None,
        system_prompt=(system_prompt or None) if system_prompt is None else system_prompt.strip() or None,
        interface_config=interface_config,
        preferences=preferences,
        is_active=True,
        tool_permissions=[],
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)

    from src.shared.cache.permissions_cache import (  # noqa: PLC0415
        get_perm_redis_client,
        invalidate_org_permissions,
    )
    rc = get_perm_redis_client()
    if rc is not None:
        await invalidate_org_permissions(rc, org_id)

    return _profile_to_dict(profile)


async def update_interface_profile(
    db: AsyncSession,
    profile_id: uuid.UUID,
    **fields: Any,
) -> Optional[dict[str, Any]]:
    from src.shared.models.interface_profile import GSageInterfaceProfile  # noqa: PLC0415

    await db.execute(
        update(GSageInterfaceProfile)
        .where(GSageInterfaceProfile.id == profile_id)
        .values(**fields)
    )
    await db.commit()
    result = await db.execute(
        select(GSageInterfaceProfile).where(GSageInterfaceProfile.id == profile_id)
    )
    p = result.scalar_one_or_none()

    if p is not None:
        from src.shared.cache.permissions_cache import (  # noqa: PLC0415
            get_perm_redis_client,
            invalidate_org_permissions,
        )
        rc = get_perm_redis_client()
        if rc is not None:
            await invalidate_org_permissions(rc, p.org_id)

    return _profile_to_dict(p) if p else None


async def delete_interface_profile(
    db: AsyncSession,
    profile_id: uuid.UUID,
) -> bool:
    from src.shared.models.interface_profile import GSageInterfaceProfile  # noqa: PLC0415

    # Fetch org_id before delete for cache invalidation
    result = await db.execute(
        select(GSageInterfaceProfile).where(GSageInterfaceProfile.id == profile_id)
    )
    p = result.scalar_one_or_none()
    if p is None:
        return False
    org_id = p.org_id

    await db.execute(
        delete(GSageInterfaceProfile).where(GSageInterfaceProfile.id == profile_id)
    )
    await db.commit()

    from src.shared.cache.permissions_cache import (  # noqa: PLC0415
        get_perm_redis_client,
        invalidate_org_permissions,
    )
    rc = get_perm_redis_client()
    if rc is not None:
        await invalidate_org_permissions(rc, org_id)

    return True


def _tool_config_to_dict(tc: Any) -> dict[str, Any]:
    try:
        config = tc.config  # property decrypts
    except Exception:
        config = {}
    return {
        "id": str(tc.id),
        "org_id": str(tc.org_id),
        "tool_name": tc.tool_name,
        "profile_id": tc.profile_id,
        "description": tc.description or "",
        "config": config,
        "updated_at": tc.updated_at.isoformat() if tc.updated_at else "",
    }


def _profile_to_dict(p: Any) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "org_id": str(p.org_id),
        "dept_id": str(p.dept_id) if p.dept_id else None,
        "interface": p.interface,
        "is_active": p.is_active,
        "mode": p.mode,
        "description": p.description or "",
        "system_prompt": p.system_prompt or "",
        "tool_permissions": p.tool_permissions or [],
        "interface_config": p.interface_config or {},
        "preferences": p.preferences or {},
        "updated_at": p.updated_at.isoformat() if p.updated_at else "",
    }
