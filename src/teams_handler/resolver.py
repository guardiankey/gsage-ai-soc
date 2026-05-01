"""gSage AI — Microsoft Teams sender resolver.

Resolution order (per inbound activity):

  1. ``GSageUser.teams_aad_object_id`` lookup, scoped to ``org_id``.
  2. Microsoft Graph fallback (``aad_object_id`` → primary e-mail) →
     match against ``GSageUser.email`` in the same org.
  3. On match, persist the AAD Object ID to ``GSageUser`` so step 1
     resolves immediately on subsequent messages.

Returns ``None`` when the sender cannot be linked to any active
``GSageUser`` in the target org.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.user import GSageUser
from src.shared.models.user_organization import GSageUserOrganization
from src.teams_handler.graph_client import GraphClient

logger = logging.getLogger(__name__)


async def resolve_teams_sender(
    session: AsyncSession,
    *,
    aad_object_id: str,
    org_id: uuid.UUID,
    graph: Optional[GraphClient] = None,
) -> Optional[GSageUser]:
    """Resolve a Teams ``aadObjectId`` to a ``GSageUser`` within *org_id*.

    Args:
        session:        Async SQLAlchemy session.
        aad_object_id:  GUID from ``activity.from.aadObjectId``.
        org_id:         Organization UUID for tenant isolation.
        graph:          Optional ``GraphClient`` enabling first-contact
                        e-mail fallback. Pass ``None`` to disable.

    Returns:
        Matching ``GSageUser``, or ``None`` if no link could be made.
    """
    aad_id = str(aad_object_id).strip()
    if not aad_id:
        return None

    # ── 1. Direct AAD lookup ────────────────────────────────────────
    user = await _find_by_aad_id(session, aad_id, org_id)
    if user is not None:
        return user

    # ── 2. Graph fallback ──────────────────────────────────────────
    if graph is None:
        logger.debug(
            "resolve_teams_sender: no DB match and Graph fallback disabled "
            "— aad_id=%s org_id=%s",
            aad_id,
            org_id,
        )
        return None

    email = await graph.lookup_email(aad_id)
    if not email:
        logger.info(
            "resolve_teams_sender: graph could not resolve email — aad_id=%s",
            aad_id,
        )
        return None

    user = await _find_by_email(session, email, org_id)
    if user is None:
        logger.info(
            "resolve_teams_sender: graph email %s not registered in org=%s",
            email,
            org_id,
        )
        return None

    # ── 3. Persist AAD ID for future direct hits ───────────────────
    user.teams_aad_object_id = aad_id
    await session.flush()
    logger.info(
        "resolve_teams_sender: linked teams_aad_object_id=%s to user_id=%s "
        "via graph email match",
        aad_id,
        user.id,
    )
    return user


async def _find_by_aad_id(
    session: AsyncSession, aad_id: str, org_id: uuid.UUID
) -> Optional[GSageUser]:
    stmt = (
        select(GSageUser)
        .join(GSageUserOrganization, GSageUserOrganization.user_id == GSageUser.id)
        .where(
            GSageUserOrganization.org_id == org_id,
            GSageUser.teams_aad_object_id == aad_id,
            GSageUser.is_active == True,  # noqa: E712
        )
    )
    return (await session.execute(stmt)).scalars().first()


async def _find_by_email(
    session: AsyncSession, email: str, org_id: uuid.UUID
) -> Optional[GSageUser]:
    # Microsoft Graph returns the e-mail as configured, which may differ in
    # case from what the org's admin typed. Compare case-insensitively.
    from sqlalchemy import func

    stmt = (
        select(GSageUser)
        .join(GSageUserOrganization, GSageUserOrganization.user_id == GSageUser.id)
        .where(
            GSageUserOrganization.org_id == org_id,
            func.lower(GSageUser.email) == email.lower(),
            GSageUser.is_active == True,  # noqa: E712
        )
    )
    return (await session.execute(stmt)).scalars().first()
