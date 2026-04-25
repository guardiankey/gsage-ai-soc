"""gSage AI — MCP Server permission resolution.

Resolves a user's **tool-level** permissions (e.g. ``dns:read``,
``whois:read``) by querying the database:

    User → Groups (scoped to org) → Permissions → tags

Optionally applies **interface-level filtering** using
:class:`~src.shared.models.interface_profile.GSageInterfaceProfile`
which supports ``allowlist`` and ``denylist`` modes.

This runs inside the MCP server process — zero trust, no reliance on
the backend to send permission lists.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from src.shared.cache.permissions_cache import (
    get_cached_permissions,
    set_cached_permissions,
)
from src.shared.models.group import GSageGroup, gsage_group_permissions
from src.shared.models.interface_profile import GSageInterfaceProfile
from src.shared.models.permission import GSagePermission
from src.shared.models.user import GSageUser

logger = logging.getLogger(__name__)


async def _get_interface_profile(
    session: AsyncSession,
    org_id: uuid.UUID,
    interface: str,
) -> Optional[GSageInterfaceProfile]:
    """Load the active interface profile for an org + interface pair."""
    result = await session.execute(
        select(GSageInterfaceProfile).where(
            GSageInterfaceProfile.org_id == org_id,
            GSageInterfaceProfile.interface == interface,
            GSageInterfaceProfile.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


def _apply_interface_filter(
    tags: set[str],
    profile: GSageInterfaceProfile,
) -> set[str]:
    """Narrow *tags* according to the interface profile's mode.

    * **allowlist** → intersection: only keep tags that are in the profile list.
    * **denylist**  → difference: remove tags that are in the profile list.
    """
    profile_tags = set(profile.tool_permissions or [])

    if profile.mode == "allowlist":
        filtered = tags & profile_tags
    else:  # denylist (default)
        filtered = tags - profile_tags

    logger.debug(
        "Interface filter mode=%s interface=%s: %d → %d tags (profile_tags=%s)",
        profile.mode,
        profile.interface,
        len(tags),
        len(filtered),
        sorted(profile_tags),
    )
    return filtered


async def resolve_tool_permissions(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interface: str = "web",
    dept_id: Optional[uuid.UUID] = None,
    redis_client=None,
) -> list[str]:
    """Return the list of tool permission tags for a user within an org.

    Walks:  User → user_groups → GSageGroup (filtered by org_id)
            → gsage_group_permissions (dept-aware) → GSagePermission.tag

    Department scoping rules:
    - dept_id=None  → only global permissions (dept_id IS NULL in the join table)
    - dept_id=<uuid> → global permissions UNION dept-specific permissions for that dept

    Then applies interface-level filtering if a
    :class:`GSageInterfaceProfile` exists for ``(org_id, interface)``.

    Returns an empty list if the user has no groups or no permissions.

    When ``redis_client`` is provided, results are cached in Redis for
    ``PERM_CACHE_TTL`` seconds to avoid repeated DB queries within the same
    conversation.  Explicit cache invalidation is performed by the Backend API
    and Admin Console whenever permissions change.
    """
    # ── 1. Cache check ─────────────────────────────────────────────────
    if redis_client is not None:
        cached = await get_cached_permissions(
            redis_client, org_id, user_id, interface, dept_id
        )
        if cached is not None:
            logger.debug(
                "resolve_tool_permissions CACHE HIT user=%s org=%s interface=%s → %d tags",
                user_id, org_id, interface, len(cached),
            )
            return cached

    # ── 2. DB queries ──────────────────────────────────────────────────
    async with session_factory() as session:
        # Resolve the user's groups for this org
        group_result = await session.execute(
            select(GSageGroup.id)
            .where(GSageGroup.org_id == org_id)
            .join(GSageGroup.users)
            .where(GSageUser.id == user_id)
        )
        group_ids = [row[0] for row in group_result.all()]

        if not group_ids:
            logger.debug(
                "resolve_tool_permissions: user=%s org=%s → no groups, returning empty",
                user_id, org_id,
            )
            final_tags: list[str] = []
        else:
            # Build dept-aware filter on gsage_group_permissions:
            #   dept_id=None  → only global rows (dept_id IS NULL)
            #   dept_id=<uuid> → global rows OR rows scoped to this specific dept
            if dept_id is None:
                dept_filter = gsage_group_permissions.c.dept_id.is_(None)
            else:
                dept_filter = or_(
                    gsage_group_permissions.c.dept_id.is_(None),
                    gsage_group_permissions.c.dept_id == dept_id,
                )

            perm_result = await session.execute(
                select(GSagePermission.tag)
                .join(
                    gsage_group_permissions,
                    GSagePermission.id == gsage_group_permissions.c.permission_id,
                )
                .where(
                    and_(
                        gsage_group_permissions.c.group_id.in_(group_ids),
                        dept_filter,
                    )
                )
                .distinct()
            )
            tags: set[str] = {row[0] for row in perm_result.all()}

            # ── Interface-level filtering ──────────────────────────────
            profile = await _get_interface_profile(session, org_id, interface)
            if profile is not None:
                tags = _apply_interface_filter(tags, profile)

            logger.debug(
                "Resolved %d tool permissions for user=%s org=%s dept=%s interface=%s: %s",
                len(tags), user_id, org_id, dept_id, interface, sorted(tags),
            )
            final_tags = sorted(tags)

    # ── 3. Cache store ─────────────────────────────────────────────────
    if redis_client is not None:
        await set_cached_permissions(
            redis_client, org_id, user_id, interface, dept_id, final_tags
        )

    return final_tags
