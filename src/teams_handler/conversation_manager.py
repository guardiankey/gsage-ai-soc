"""gSage AI — Microsoft Teams conversation manager.

Get/create the per-user, per-thread ``GSageChannelConversation`` row
plus a linked ``GSageTenantSession`` (memory continuity), and persist
the Bot Framework ``ConversationReference`` so we can later send
**proactive** outbound messages via ``adapter.continue_conversation``.

The conversation reference is stored as JSON inside
``GSageChannelConversation.conversation_reference``. We also stash the
``profile_id`` in that JSON under the ``_gsage_profile_id`` key so the
outbound delivery service knows which Azure Bot credentials to load.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.channel_conversation import GSageChannelConversation
from src.shared.models.tenant_session import GSageTenantSession
from src.shared.models.user import GSageUser

logger = logging.getLogger(__name__)


async def get_or_create_conversation(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user: GSageUser,
    channel_chat_id: str,
    conversation_reference: Optional[dict] = None,
    profile_id: Optional[uuid.UUID] = None,
) -> Tuple[GSageChannelConversation, GSageTenantSession, bool]:
    """Find or create a Teams conversation for *user* in *channel_chat_id*.

    *conversation_reference* is the dict returned by
    ``TurnContext.get_conversation_reference(activity)`` (already
    serialized via ``ConversationReference.serialize()`` by the caller).
    When provided, it overwrites the previously-stored reference so we
    always hold the freshest service URL / activity ID for proactive
    outbound. *profile_id* is folded into the JSON to remember which
    Azure Bot credentials this conversation uses.

    Returns ``(conversation, tenant_session, is_new)``.
    """
    stmt = select(GSageChannelConversation).where(
        GSageChannelConversation.org_id == org_id,
        GSageChannelConversation.channel == "teams",
        GSageChannelConversation.channel_chat_id == str(channel_chat_id),
        GSageChannelConversation.user_id == user.id,
    )
    conv = (await session.execute(stmt)).scalars().first()

    if conv is not None:
        # Refresh ConversationReference if the caller supplied one — the
        # `serviceUrl`, channelId or activityId may have rotated.
        if conversation_reference is not None:
            conv.conversation_reference = _augment_reference(
                conversation_reference, profile_id
            )
        tenant_session = (
            await session.get(GSageTenantSession, conv.session_id)
            if conv.session_id
            else None
        )
        if tenant_session is None:
            tenant_session = await _create_session(session, user=user, org_id=org_id)
            conv.session_id = tenant_session.id
        await session.flush()
        return conv, tenant_session, False

    # ── New conversation ────────────────────────────────────────────
    tenant_session = await _create_session(session, user=user, org_id=org_id)
    conv = GSageChannelConversation(
        org_id=org_id,
        user_id=user.id,
        channel="teams",
        channel_chat_id=str(channel_chat_id),
        session_id=tenant_session.id,
        message_count=0,
        conversation_reference=_augment_reference(
            conversation_reference, profile_id
        )
        if conversation_reference is not None
        else None,
    )
    session.add(conv)
    await session.flush()

    logger.info(
        "get_or_create_conversation (teams): new — conv_id=%s user_id=%s "
        "chat_id=%s profile_id=%s",
        conv.id,
        user.id,
        channel_chat_id,
        profile_id,
    )
    return conv, tenant_session, True


# ── Private helpers ─────────────────────────────────────────────────


def _augment_reference(
    reference: dict[str, Any], profile_id: Optional[uuid.UUID]
) -> dict[str, Any]:
    """Stash the profile_id inside the stored reference JSON."""
    enriched = dict(reference)
    if profile_id is not None:
        enriched["_gsage_profile_id"] = str(profile_id)
    return enriched


async def _create_session(
    session: AsyncSession,
    *,
    user: GSageUser,
    org_id: uuid.UUID,
) -> GSageTenantSession:
    today = date.today().strftime("%Y-%m-%d")
    tenant_session = GSageTenantSession(
        org_id=org_id,
        user_id=user.id,
        agno_session_id=f"teams_{uuid.uuid4()}",
        source="teams",
        is_active=True,
        title=f"Teams ({today})",
    )
    session.add(tenant_session)
    await session.flush()
    return tenant_session
