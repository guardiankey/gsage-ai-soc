"""API key service for admin console — direct model access (no CacheService)."""

from __future__ import annotations

import uuid
from datetime import timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


async def create_api_key(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    environment: str = "live",
    scoped_permissions: list[str] | None = None,
    interface: str | None = None,
    rate_limit_per_minute: int = 60,
) -> tuple[str, dict[str, Any]]:
    """Create a new API key, persist it, and return (raw_key, record_dict).

    The raw_key is returned exactly once — the caller must show it to the user
    immediately (e.g. via CopyDialog), as it cannot be recovered later.

    Args:
        db: Async database session.
        org_id: Organization UUID.
        name: Human-readable key name.
        environment: 'live' or 'test'.
        scoped_permissions: List of permission tags (empty = all org permissions).
        interface: UI interface hint ('api', 'web', 'cli', etc.) or None.
        rate_limit_per_minute: Max requests per minute (min 1).

    Returns:
        Tuple of (raw_key, dict with key metadata).
    """
    from src.shared.models.api_key import GSageAPIKey  # noqa: PLC0415
    from src.shared.security.auth import (  # noqa: PLC0415
        calculate_api_key_expiration,
        generate_api_key,
    )

    raw_key, key_hash, key_prefix = generate_api_key(environment)
    expires_at = calculate_api_key_expiration(years=1)

    key = GSageAPIKey(
        org_id=org_id,
        name=name.strip(),
        key_hash=key_hash,
        key_prefix=key_prefix,
        environment=environment,
        scoped_permissions=scoped_permissions or [],
        interface=interface or None,
        expires_at=expires_at,
        rate_limit_per_minute=max(1, rate_limit_per_minute),
        is_active=True,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)

    return raw_key, {
        "id": str(key.id),
        "name": key.name,
        "key_prefix": key.key_prefix,
        "environment": key.environment,
        "interface": key.interface or "",
        "scoped_permissions": key.scoped_permissions,
        "rate_limit_per_minute": key.rate_limit_per_minute,
        "expires_at": str(key.expires_at)[:10],
        "is_active": key.is_active,
    }
