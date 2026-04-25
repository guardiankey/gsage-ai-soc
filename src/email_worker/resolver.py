"""gSage AI — Email sender resolver (Phase 7).

Resolves a From: email address to a GSageUser within the organization.

Two-step lookup per PROMPT.md Phase 7 spec:
  1. users.email (primary) — exact match, case-insensitive.
  2. users.secondary_emails — scan each newline-separated line.

Both lookups are scoped to org_id (tenant isolation).

Returns None if no matching user is found (unknown sender).
The caller (Celery task) is responsible for moving the email to the
unknown_sender_folder and writing the audit log entry.
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


async def resolve_sender(
    session: AsyncSession,
    from_addr: str,
    org_id: uuid.UUID,
) -> Optional[GSageUser]:
    """Resolve *from_addr* to a GSageUser within *org_id*.

    Args:
        session:   Async SQLAlchemy session.
        from_addr: The email address from the From: header, already
                   lowercased and stripped by the parser.
        org_id:    Organization UUID for tenant isolation.

    Returns:
        The matching GSageUser, or None if not found.
    """
    from_addr = from_addr.lower().strip()
    if not from_addr:
        return None

    # ── Step 1: primary email lookup ──────────────────────────────────────
    stmt = (
        select(GSageUser)
        .join(GSageUserOrganization, GSageUserOrganization.user_id == GSageUser.id)
        .where(
            GSageUserOrganization.org_id == org_id,
            GSageUser.email == from_addr,
            GSageUser.is_active == True,  # noqa: E712 — SQLAlchemy uses ==
        )
    )
    result = await session.execute(stmt)
    user = result.scalars().first()
    if user is not None:
        logger.debug(
            "resolve_sender: primary match — from=%s user_id=%s",
            from_addr,
            user.id,
        )
        return user

    # ── Step 2: secondary_emails scan ─────────────────────────────────────
    # secondary_emails is a Text column with one address per line (max 5).
    # We load all users in the org that have secondary_emails defined and
    # check each line. This is acceptable given the per-org user counts
    # (typically < 50 users) and the email queue throughput.
    stmt2 = (
        select(GSageUser)
        .join(GSageUserOrganization, GSageUserOrganization.user_id == GSageUser.id)
        .where(
            GSageUserOrganization.org_id == org_id,
            GSageUser.is_active == True,  # noqa: E712
            GSageUser.secondary_emails.is_not(None),
        )
    )
    result2 = await session.execute(stmt2)
    candidates = result2.scalars().all()

    for candidate in candidates:
        raw = (candidate.secondary_emails or "").strip()
        if not raw:
            continue
        for line in raw.splitlines():
            alt = line.strip().lower()
            if alt and alt == from_addr:
                logger.debug(
                    "resolve_sender: secondary match — from=%s user_id=%s",
                    from_addr,
                    candidate.id,
                )
                return candidate

    logger.info(
        "resolve_sender: unknown sender — from=%s org_id=%s",
        from_addr,
        org_id,
    )
    return None
