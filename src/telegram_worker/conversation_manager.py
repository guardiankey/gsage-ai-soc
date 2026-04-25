"""gSage AI — Telegram conversation manager.

Handles finding or creating channel conversations and their linked
GSageTenantSessions for the Telegram channel.

The unique key for a conversation is (org_id, channel, channel_chat_id, user_id).
One GSageTenantSession is created per conversation and reused on subsequent
messages so the agent retains full conversational memory.

Returns:
  tuple[GSageChannelConversation, GSageTenantSession, bool]
  The bool is True when a new conversation was created.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.channel_conversation import GSageChannelConversation
from src.shared.models.tenant_session import GSageTenantSession
from src.shared.models.user import GSageUser

logger = logging.getLogger(__name__)


async def get_or_create_conversation(
    session: AsyncSession,
    org_id: uuid.UUID,
    user: GSageUser,
    channel_chat_id: str,
    channel: str = "telegram",
) -> Tuple[GSageChannelConversation, GSageTenantSession, bool]:
    """Find or create a channel conversation for *user* in *channel_chat_id*.

    Args:
        session:         Async SQLAlchemy session.
        org_id:          Organization UUID (tenant isolation).
        user:            Resolved GSageUser.
        channel_chat_id: Channel-native chat identifier (e.g. Telegram chat_id).
        channel:         Channel name in lowercase (default ``"telegram"``).

    Returns:
        (conversation, tenant_session, is_new)
    """
    stmt = select(GSageChannelConversation).where(
        GSageChannelConversation.org_id == org_id,
        GSageChannelConversation.channel == channel,
        GSageChannelConversation.channel_chat_id == str(channel_chat_id),
        GSageChannelConversation.user_id == user.id,
    )
    result = await session.execute(stmt)
    conv = result.scalars().first()

    if conv is not None:
        # Load linked session
        tenant_session = await session.get(GSageTenantSession, conv.session_id) if conv.session_id else None
        if tenant_session:
            logger.debug(
                "get_or_create_conversation: existing — conv_id=%s session_id=%s",
                conv.id,
                tenant_session.id,
            )
            return conv, tenant_session, False

        # Session was deleted — recreate it
        tenant_session = await _create_session(session, user=user, org_id=org_id, channel=channel)
        conv.session_id = tenant_session.id
        await session.flush()
        return conv, tenant_session, False

    # Create new session + conversation
    tenant_session = await _create_session(session, user=user, org_id=org_id, channel=channel)
    conv = GSageChannelConversation(
        org_id=org_id,
        user_id=user.id,
        channel=channel,
        channel_chat_id=str(channel_chat_id),
        session_id=tenant_session.id,
        message_count=0,
    )
    session.add(conv)
    await session.flush()

    logger.info(
        "get_or_create_conversation: new — conv_id=%s user_id=%s channel=%s chat_id=%s",
        conv.id,
        user.id,
        channel,
        channel_chat_id,
    )
    return conv, tenant_session, True


# ── Private helpers ────────────────────────────────────────────────────────


async def _create_session(
    session: AsyncSession,
    *,
    user: GSageUser,
    org_id: uuid.UUID,
    channel: str,
) -> GSageTenantSession:
    today = date.today().strftime("%Y-%m-%d")
    channel_label = channel.capitalize()
    tenant_session = GSageTenantSession(
        org_id=org_id,
        user_id=user.id,
        agno_session_id=f"{channel}_{uuid.uuid4()}",
        source=channel,
        is_active=True,
        title=f"{channel_label} ({today})",
    )
    session.add(tenant_session)
    await session.flush()
    return tenant_session
