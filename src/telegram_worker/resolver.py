"""gSage AI — Telegram sender resolver.

Resolves a Telegram user ID (numeric string) to a GSageUser within
the organization, using the ``telegram_id`` field added to GSageUser.

Scoped to ``org_id`` for full multi-tenant isolation.
Returns None if no matching user is found (unknown sender).
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.user import GSageUser
from src.shared.models.user_organization import GSageUserOrganization

logger = logging.getLogger(__name__)


async def resolve_telegram_sender(
    session: AsyncSession,
    telegram_user_id: str,
    org_id: uuid.UUID,
) -> Optional[GSageUser]:
    """Resolve *telegram_user_id* to a GSageUser within *org_id*.

    Args:
        session:          Async SQLAlchemy session.
        telegram_user_id: Telegram numeric user ID as a string.
        org_id:           Organization UUID for tenant isolation.

    Returns:
        The matching GSageUser, or None if not found.
    """
    tg_id = str(telegram_user_id).strip()
    if not tg_id:
        return None

    stmt = (
        select(GSageUser)
        .join(GSageUserOrganization, GSageUserOrganization.user_id == GSageUser.id)
        .where(
            GSageUserOrganization.org_id == org_id,
            GSageUser.telegram_id == tg_id,
            GSageUser.is_active == True,  # noqa: E712 — SQLAlchemy uses ==
        )
    )
    result = await session.execute(stmt)
    user = result.scalars().first()

    if user is not None:
        logger.debug(
            "resolve_telegram_sender: match — telegram_id=%s user_id=%s",
            tg_id,
            user.id,
        )
    else:
        logger.debug(
            "resolve_telegram_sender: no match — telegram_id=%s org_id=%s",
            tg_id,
            org_id,
        )

    return user
