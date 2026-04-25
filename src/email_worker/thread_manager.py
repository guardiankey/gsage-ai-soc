"""gSage AI — Email thread & session manager (Phase 7).

Handles finding or creating email threads and their linked tenant sessions.

Thread matching logic (per PROMPT.md Phase 7):
  1. In-Reply-To header → look up message_id in gsage_email_messages.
     If found → use existing thread.
  2. Fallback → normalized_subject + from_addr lookup in gsage_email_threads
     (same org, same user).
  3. If no match → create new GSageEmailThread + new GSageTenantSession.

Anti-spoofing:
  If the email claims to be a reply (has In-Reply-To) but the referenced
  Message-ID is NOT found in the org's messages table, treat it as a new
  thread and log an audit warning.  Never silently accept an In-Reply-To
  that points to a message from another org.

Returns:
  tuple[GSageEmailThread, GSageTenantSession, bool]
  The bool is True when a new thread was created (for rate-limit purposes).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.tenant_session import GSageTenantSession
from src.shared.models.email_message import GSageEmailMessage
from src.shared.models.email_thread import GSageEmailThread
from src.shared.models.email_account import GSageEmailAccount
from src.shared.models.user import GSageUser
from src.email_worker.parser import ParsedEmail

logger = logging.getLogger(__name__)


async def get_or_create_thread(
    session: AsyncSession,
    parsed: ParsedEmail,
    user: GSageUser,
    account: GSageEmailAccount,
) -> Tuple[GSageEmailThread, GSageTenantSession, bool]:
    """Find an existing thread or create a new one for *parsed*.

    Args:
        session: Async SQLAlchemy session.
        parsed:  Parsed email metadata.
        user:    Resolved sender (GSageUser).
        account: Email account receiving this email (provides org_id).

    Returns:
        (thread, tenant_session, is_new_thread)
    """
    org_id: uuid.UUID = account.org_id

    # ── Step 1: In-Reply-To lookup ────────────────────────────────────────
    if parsed.in_reply_to:
        thread, tenant_session = await _find_by_in_reply_to(
            session=session,
            in_reply_to=parsed.in_reply_to,
            org_id=org_id,
        )
        if thread and tenant_session:
            logger.debug(
                "get_or_create_thread: found via In-Reply-To — thread_id=%s",
                thread.id,
            )
            return thread, tenant_session, False
        elif thread is None and parsed.in_reply_to:
            # Anti-spoofing: suspicious In-Reply-To header not in our DB.
            logger.warning(
                "get_or_create_thread: anti-spoof — In-Reply-To not found in org — "
                "from=%s in_reply_to=%s org_id=%s; treating as new thread",
                parsed.from_addr,
                parsed.in_reply_to,
                org_id,
            )
            # Fall through to create new thread (do not reply to unknown chain).

    # ── Step 2: Subject + sender fallback ────────────────────────────────
    thread_row = await _find_by_subject_sender(
        session=session,
        normalized_subject=parsed.normalized_subject,
        from_addr=parsed.from_addr,
        user_id=user.id,
        org_id=org_id,
    )
    if thread_row:
        tenant_session = await _load_session(session, thread_row.session_id)
        if tenant_session:
            logger.debug(
                "get_or_create_thread: found via subject fallback — thread_id=%s",
                thread_row.id,
            )
            return thread_row, tenant_session, False
        # Session was deleted — create a new one for existing thread
        tenant_session = await _create_session(session, user=user, org_id=org_id)
        thread_row.session_id = tenant_session.id
        await session.flush()
        return thread_row, tenant_session, False

    # ── Step 3: Create new thread + session ──────────────────────────────
    tenant_session = await _create_session(session, user=user, org_id=org_id)
    thread = GSageEmailThread(
        org_id=org_id,
        user_id=user.id,
        thread_subject=parsed.normalized_subject[:500],
        session_id=tenant_session.id,
        message_count=0,
    )
    session.add(thread)
    await session.flush()
    logger.info(
        "get_or_create_thread: new thread — thread_id=%s user_id=%s",
        thread.id,
        user.id,
    )
    return thread, tenant_session, True


# ── Private helpers ────────────────────────────────────────────────────────


async def _find_by_in_reply_to(
    *,
    session: AsyncSession,
    in_reply_to: str,
    org_id: uuid.UUID,
) -> Tuple[Optional[GSageEmailThread], Optional[GSageTenantSession]]:
    """Find a thread via a known In-Reply-To Message-ID header.

    Only looks at messages belonging to *org_id* (tenant safety).
    """
    # Find the email message that has this Message-ID.
    stmt = select(GSageEmailMessage).where(
        GSageEmailMessage.message_id == in_reply_to,
        GSageEmailMessage.org_id == org_id,
    )
    result = await session.execute(stmt)
    msg = result.scalars().first()
    if msg is None or msg.thread_id is None:
        return None, None

    # Load thread.
    thread = await session.get(GSageEmailThread, msg.thread_id)
    if thread is None:
        return None, None

    # Load session.
    tenant_session = (
        await _load_session(session, thread.session_id)
        if thread.session_id
        else None
    )
    return thread, tenant_session


async def _find_by_subject_sender(
    *,
    session: AsyncSession,
    normalized_subject: str,
    from_addr: str,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
) -> Optional[GSageEmailThread]:
    """Find a thread by normalized subject + sender (intra-org fallback)."""
    stmt = (
        select(GSageEmailThread)
        .where(
            GSageEmailThread.org_id == org_id,
            GSageEmailThread.user_id == user_id,
            GSageEmailThread.thread_subject == normalized_subject[:500],
        )
        .order_by(GSageEmailThread.created_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def _load_session(
    session: AsyncSession,
    session_id: Optional[uuid.UUID],
) -> Optional[GSageTenantSession]:
    if session_id is None:
        return None
    return await session.get(GSageTenantSession, session_id)


async def _create_session(
    session: AsyncSession,
    *,
    user: GSageUser,
    org_id: uuid.UUID,
) -> GSageTenantSession:
    # Two-step creation so that agno_session_id follows the canonical
    # tenant-scoped format ``org_<org_id>:email-conv:<session_id>`` from
    # the start. This keeps the Agno post-hook (persist_agno_run_projection)
    # pointing at THIS row instead of creating a second phantom session.
    date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    tenant_session = GSageTenantSession(
        org_id=org_id,
        user_id=user.id,
        # Temporary unique placeholder; overwritten below once we have the PK.
        agno_session_id=f"pending_{uuid.uuid4()}",
        source="email",
        is_active=True,
        title=f"E-mail ({date_str})",
    )
    session.add(tenant_session)
    await session.flush()
    tenant_session.agno_session_id = (
        f"org_{org_id}:email-conv:{tenant_session.id}"
    )
    await session.flush()
    return tenant_session
