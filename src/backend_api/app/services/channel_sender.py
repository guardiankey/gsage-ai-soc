"""Channel delivery dispatcher — routes agent responses to the correct output.

After the Agent Continuation Service produces a response, this module delivers
it to the originating channel:

* **web** — persist as an assistant message so React Query polling picks it up.
* **telegram** — look up the ChannelConversation, load the bot token from the
  InterfaceProfile, and send via ``telegram.Bot.send_message()``.
* **scheduled** — update ScheduledJob.last_run_result (if linked).
* **cli / api / email** — no-op (pull on next request / future).
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.tenant_session import GSageTenantSession

log = logging.getLogger(__name__)


async def deliver_response(
    session: GSageTenantSession,
    text: str,
    db: AsyncSession,
) -> None:
    """Dispatch *text* to the channel that owns *session*.

    Dispatches by ``session.source``.  Errors are logged but do not propagate.
    """
    source = session.source or "web"
    try:
        if source == "telegram":
            await _deliver_telegram(session, text, db)
        elif source == "teams":
            await _deliver_teams(session, text, db)
        elif source == "web":
            await _deliver_web(session, text, db)
        elif source == "scheduled":
            await _deliver_scheduled(session, text, db)
        elif source == "email":
            await _deliver_email(session, text, db)
        else:
            log.debug(
                "channel_sender: no push delivery for source=%s session=%s",
                source, session.id,
            )
    except Exception as exc:
        log.error(
            "channel_sender: delivery failed source=%s session=%s: %s",
            source, session.id, exc, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Web: persist assistant message for polling
# ---------------------------------------------------------------------------

async def _deliver_web(
    session: GSageTenantSession,
    text: str,
    db: AsyncSession,
) -> None:
    """For web sessions the response is already persisted by the Agno post-hook.

    The frontend polls via React Query (refetchInterval 5s) and will pick up
    the new message on the next cycle.  No additional action is needed here,
    but we log for traceability.
    """
    log.info(
        "channel_sender[web]: response persisted via Agno post-hook, session=%s len=%d",
        session.id, len(text),
    )


# ---------------------------------------------------------------------------
# Telegram: send message to the linked chat
# ---------------------------------------------------------------------------

async def _deliver_telegram(
    session: GSageTenantSession,
    text: str,
    db: AsyncSession,
) -> None:
    """Send *text* to the Telegram chat linked to *session*."""
    from src.shared.models.channel_conversation import GSageChannelConversation
    from src.shared.models.interface_profile import GSageInterfaceProfile
    from src.telegram_worker.formatting import (
        DEFAULT_MAX_LEN,
        markdown_to_telegram_html,
        split_text,
    )

    # 1. Find the ChannelConversation for this session
    conv_result = await db.execute(
        select(GSageChannelConversation).where(
            GSageChannelConversation.session_id == session.id,
            GSageChannelConversation.channel == "telegram",
        )
    )
    conversation = conv_result.scalar_one_or_none()
    if conversation is None:
        log.warning(
            "channel_sender[telegram]: no ChannelConversation for session=%s",
            session.id,
        )
        return

    chat_id = conversation.channel_chat_id

    # 2. Find the InterfaceProfile with a bot_token for this org's Telegram
    profile_result = await db.execute(
        select(GSageInterfaceProfile).where(
            GSageInterfaceProfile.org_id == session.org_id,
            GSageInterfaceProfile.interface == "telegram",
            GSageInterfaceProfile.is_active == True,  # noqa: E712
        )
    )
    profile = profile_result.scalars().first()
    if profile is None:
        log.warning(
            "channel_sender[telegram]: no active telegram profile for org=%s",
            session.org_id,
        )
        return

    cfg = profile.interface_config or {}
    bot_token = cfg.get("bot_token", "").strip()
    if not bot_token:
        log.warning(
            "channel_sender[telegram]: profile %s has no bot_token",
            profile.id,
        )
        return

    # 3. Send via telegram.Bot (standalone, no Application needed)
    from telegram import Bot

    bot = Bot(token=bot_token)
    html_text = markdown_to_telegram_html(text)
    chunks = split_text(html_text, DEFAULT_MAX_LEN)
    for chunk in chunks:
        await bot.send_message(
            chat_id=int(chat_id),
            text=chunk,
            parse_mode="HTML",
        )

    log.info(
        "channel_sender[telegram]: sent %d chunk(s) to chat=%s session=%s",
        len(chunks), chat_id, session.id,
    )


# ---------------------------------------------------------------------------
# Microsoft Teams: proactive message via Bot Framework continue_conversation
# ---------------------------------------------------------------------------

async def _deliver_teams(
    session: GSageTenantSession,
    text: str,
    db: AsyncSession,
) -> None:
    """Send *text* to the Teams conversation linked to *session*.

    Uses the Bot Framework ``continue_conversation`` flow with the
    ``ConversationReference`` previously persisted on
    ``GSageChannelConversation.conversation_reference``.
    """
    from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, MessageFactory
    from botbuilder.schema import ConversationReference

    from src.shared.models.channel_conversation import GSageChannelConversation
    from src.shared.models.interface_profile import GSageInterfaceProfile
    from src.teams_handler.formatting import DEFAULT_MAX_LEN, split_text

    # 1. Locate the Teams conversation for this session.
    conv_result = await db.execute(
        select(GSageChannelConversation).where(
            GSageChannelConversation.session_id == session.id,
            GSageChannelConversation.channel == "teams",
        )
    )
    conversation = conv_result.scalar_one_or_none()
    if conversation is None:
        log.warning(
            "channel_sender[teams]: no ChannelConversation for session=%s",
            session.id,
        )
        return

    ref_dict = conversation.conversation_reference or {}
    if not ref_dict:
        log.warning(
            "channel_sender[teams]: conversation %s has no stored "
            "ConversationReference (proactive delivery requires one)",
            conversation.id,
        )
        return

    # 2. Resolve the Azure Bot credentials. We prefer the profile id stashed
    # by conversation_manager, falling back to the org's only active Teams
    # profile.
    profile_id_str = ref_dict.get("_gsage_profile_id")
    profile: Optional[GSageInterfaceProfile] = None
    if profile_id_str:
        try:
            profile = await db.get(
                GSageInterfaceProfile, uuid.UUID(profile_id_str)
            )
        except (ValueError, TypeError):
            profile = None
    if profile is None:
        prof_result = await db.execute(
            select(GSageInterfaceProfile).where(
                GSageInterfaceProfile.org_id == session.org_id,
                GSageInterfaceProfile.interface == "teams",
                GSageInterfaceProfile.is_active == True,  # noqa: E712
            )
        )
        profile = prof_result.scalars().first()
    if profile is None:
        log.warning(
            "channel_sender[teams]: no active teams profile for org=%s",
            session.org_id,
        )
        return

    cfg = profile.interface_config or {}
    app_id = (cfg.get("app_id") or "").strip()
    app_password = (cfg.get("app_password") or "").strip()
    if not (app_id and app_password):
        log.warning(
            "channel_sender[teams]: profile %s missing credentials",
            profile.id,
        )
        return

    # 3. Build the adapter and replay-send via continue_conversation.
    bf_settings = BotFrameworkAdapterSettings(
        app_id=app_id, app_password=app_password
    )
    adapter = BotFrameworkAdapter(bf_settings)
    # Drop our private key before deserializing into ConversationReference,
    # otherwise botbuilder will reject the unknown attribute.
    clean_ref_dict = {k: v for k, v in ref_dict.items() if not k.startswith("_gsage_")}
    reference = ConversationReference().deserialize(clean_ref_dict)

    chunks = split_text(text, DEFAULT_MAX_LEN)

    async def _callback(turn_context):
        for chunk in chunks:
            msg = MessageFactory.text(chunk)
            msg.text_format = "markdown"
            await turn_context.send_activity(msg)

    await adapter.continue_conversation(reference, _callback, app_id)

    log.info(
        "channel_sender[teams]: sent %d chunk(s) to conv=%s session=%s",
        len(chunks), conversation.id, session.id,
    )


# ---------------------------------------------------------------------------
# Scheduled: update job result
# ---------------------------------------------------------------------------

async def _deliver_scheduled(
    session: GSageTenantSession,
    text: str,
    db: AsyncSession,
) -> None:
    """Update the ScheduledJob's last_run_result with the continuation output.

    Scheduled sessions use agno_session_id = ``sched_<job_id>``.
    """
    from src.shared.models.scheduled_job import GSageScheduledJob

    agno_sid = session.agno_session_id or ""
    if agno_sid.startswith("sched_"):
        job_id = agno_sid[len("sched_"):]
        try:
            job_uuid = uuid.UUID(job_id)
        except ValueError:
            log.warning(
                "channel_sender[scheduled]: invalid job_id in agno_session_id=%s",
                agno_sid,
            )
            return

        result = await db.execute(
            select(GSageScheduledJob).where(GSageScheduledJob.id == job_uuid)
        )
        job = result.scalar_one_or_none()
        if job:
            existing = job.last_run_result or {}
            existing["continuation_output"] = text[:4000]
            job.last_run_result = existing
            await db.commit()
            log.info(
                "channel_sender[scheduled]: updated job=%s with continuation output",
                job_id,
            )
        else:
            log.warning(
                "channel_sender[scheduled]: job %s not found",
                job_id,
            )
    else:
        log.debug(
            "channel_sender[scheduled]: session %s agno_sid=%s does not follow sched_ pattern",
            session.id, agno_sid,
        )


# ---------------------------------------------------------------------------
# Email: send SMTP reply to the thread that originated the session
# ---------------------------------------------------------------------------

async def _deliver_email(
    session: GSageTenantSession,
    text: str,
    db: AsyncSession,
) -> None:
    """Send *text* as an SMTP reply to the email thread linked to *session*.

    Looks up the GSageEmailThread whose session_id matches, then loads
    the most recent inbound message to obtain the correct reply headers.
    """
    from src.shared.models.email_account import GSageEmailAccount
    from src.shared.models.email_message import (
        GSageEmailDirection,
        GSageEmailMessage,
        GSageEmailStatus,
    )
    from src.shared.models.email_thread import GSageEmailThread
    from src.email_worker.smtp_sender import send_reply

    # 1. Find the email thread linked to this session
    thread_result = await db.execute(
        select(GSageEmailThread).where(
            GSageEmailThread.session_id == session.id,
        )
    )
    thread = thread_result.scalar_one_or_none()
    if thread is None:
        log.warning(
            "channel_sender[email]: no GSageEmailThread for session=%s",
            session.id,
        )
        return

    # 2. Load most recent inbound message to get reply headers and account
    msg_result = await db.execute(
        select(GSageEmailMessage)
        .where(
            GSageEmailMessage.thread_id == thread.id,
            GSageEmailMessage.direction == GSageEmailDirection.INBOUND,
        )
        .order_by(GSageEmailMessage.created_at.desc())
        .limit(1)
    )
    last_inbound = msg_result.scalar_one_or_none()
    if last_inbound is None:
        log.warning(
            "channel_sender[email]: no inbound messages in thread=%s session=%s",
            thread.id, session.id,
        )
        return

    # 3. Load the sending email account
    account = await db.get(GSageEmailAccount, last_inbound.email_account_id)
    if account is None:
        log.error(
            "channel_sender[email]: email account %s not found for thread=%s",
            last_inbound.email_account_id, thread.id,
        )
        return

    # 4. Send via SMTP
    outbound_msg_id = await send_reply(
        account=account,
        to_addr=last_inbound.from_addr,
        subject=last_inbound.subject,
        body_text=text,
        in_reply_to=last_inbound.message_id,
        references=last_inbound.references,
    )

    # 5. Persist outbound message record
    outbound = GSageEmailMessage(
        org_id=session.org_id,
        user_id=session.user_id,
        email_account_id=last_inbound.email_account_id,
        message_id=outbound_msg_id,
        in_reply_to=last_inbound.message_id,
        references=(
            ((last_inbound.references or "").strip() + " " + last_inbound.message_id).strip()
        ),
        direction=GSageEmailDirection.OUTBOUND,
        status=GSageEmailStatus.COMPLETED,
        from_addr=last_inbound.to_addr,
        to_addr=last_inbound.from_addr,
        subject=last_inbound.subject,
        body_text=text,
        thread_id=thread.id,
        session_id=session.id,
    )
    db.add(outbound)
    await db.commit()

    log.info(
        "channel_sender[email]: sent reply to=%s thread=%s session=%s",
        last_inbound.from_addr, thread.id, session.id,
    )
