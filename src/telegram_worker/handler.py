"""gSage AI — Telegram message handler.

Processing pipeline for each inbound Telegram message:
  1. Extract chat_id, telegram_user_id, text from the Update.
  2. Check for a configured InterfaceProfile that owns this bot_token.
  3. Resolve sender via ``resolver.resolve_telegram_sender()``.
  4. If unknown → reply with a not-registered notice and abort.
  5. Org-level rate limit check.
  6. User-level rate limit check.
  7. Show typing indicator (ChatAction.TYPING).
  8. Get/create GSageChannelConversation + GSageTenantSession.
  9. Persist inbound GSageChannelMessage (status=PROCESSING).
  10. Load org, membership role, InterfaceProfiles.
  11. Build TenantContext → build_agent() → agent.arun(text).
  12. Split response into ≤ max_length chunks, reply each.
  13. Persist outbound GSageChannelMessage + mark inbound COMPLETED.
  14. On errors: mark inbound FAILED, reply with a generic error notice.

``build_application()`` is called by main.py to create an ``Application``
instance with this handler registered.
"""

from __future__ import annotations

import logging
from typing import Any

from src.telegram_worker.formatting import (
    markdown_to_telegram_html as _markdown_to_telegram_html,
    split_text as _split_text,
    DEFAULT_MAX_LEN as _DEFAULT_MAX_LEN,
)

logger = logging.getLogger(__name__)


def build_application(bot_token: str, profiles: list):
    """Create and configure a python-telegram-bot Application for *bot_token*.

    Args:
        bot_token: Telegram Bot API token.
        profiles:  List of active GSageInterfaceProfile objects for this token.

    Returns:
        A configured ``telegram.ext.Application`` (not yet started).
    """
    from telegram.ext import Application, MessageHandler, filters

    app = (
        Application.builder()
        .token(bot_token)
        .build()
    )

    # Attach the profile list to bot_data for access inside the handler.
    app.bot_data["profiles"] = profiles

    # Register handler for all private text messages (and group messages).
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    return app


async def handle_message(update: Any, context: Any) -> None:
    """Handle a single inbound Telegram text message."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from src.shared.config.settings import get_settings
    from src.shared.models.channel_conversation import GSageChannelConversation
    from src.shared.models.channel_message import (
        GSageChannelDirection,
        GSageChannelMessage,
        GSageChannelStatus,
    )
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.user_organization import GSageUserOrganization
    from src.telegram_worker.conversation_manager import get_or_create_conversation
    from src.telegram_worker.rate_limiter import (
        check_org_telegram_rate,
        check_user_telegram_rate,
    )
    from src.telegram_worker.resolver import resolve_telegram_sender
    from src.backend_api.app.core.tenant import TenantContext, permissions_for_role
    from src.backend_api.app.services.agent_factory import build_agent, load_interface_profiles
    from src.backend_api.app.services.background_tasks import (
        get_pending_bg_notifications,
        build_bg_context_block,
        mark_bg_tasks_notified,
        load_dept_name,
        build_dept_context_block,
    )

    import redis.asyncio as aioredis

    # ── 1. Extract basic Telegram fields ──────────────────────────────────
    if update.message is None or update.message.text is None:
        return  # Not a text message (ignore)

    tg_user = update.effective_user
    tg_chat = update.effective_chat
    tg_message = update.message

    telegram_user_id = str(tg_user.id) if tg_user else None
    chat_id = str(tg_chat.id) if tg_chat else None
    channel_message_id = str(tg_message.message_id)
    text = tg_message.text.strip()

    if not telegram_user_id or not chat_id or not text:
        return

    # ── 2. Identify the org from the bot's InterfaceProfile ───────────────
    profiles: list = context.bot_data.get("profiles", [])
    if not profiles:
        logger.error(
            "handle_message: no profiles attached to bot — cannot route message"
        )
        return

    # Use the first profile to get org_id.  All profiles in one Application
    # share the same bot_token, hence the same org(s).  Typically one org per token.
    profile = profiles[0]
    org_id = profile.org_id

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    AsyncSession_ = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Tracks the persisted inbound message ID across transaction phases.
    # Set after Phase 1 commits so the error handler can mark it FAILED
    # even if Phase 2 or Phase 3 fail.
    inbound_msg_id = None
    # Track the agent so we can run MCP cleanup on all exit paths
    # (prevents anyio _deliver_cancellation busy-loop at 100% CPU).
    agent: Any = None

    try:
        # ═══════════════════════════════════════════════════════════════════════
        # PHASE 1 — short transaction: resolve user, create session, persist
        # inbound message, then COMMIT so GSageTenantSession is visible to
        # all DB connections before agent.arun() runs.
        #
        # Without this split, the old single long transaction would flush
        # (but not commit) the GSageTenantSession row, then call
        # agent.arun().  Agno's post-hook opens a separate DB connection,
        # can't see the uncommitted row via MVCC, and tries to INSERT the
        # same agno_session_id — deadlocking on PostgreSQL's unique-constraint
        # lock held by our still-open transaction.
        # ═══════════════════════════════════════════════════════════════════════
        async with AsyncSession_() as session:
            async with session.begin():
                # ── 3. Resolve sender ──────────────────────────────────────
                user = await resolve_telegram_sender(session, telegram_user_id, org_id)
                if user is None:
                    logger.warning(
                        "handle_message: unknown sender — tg_id=%s org_id=%s",
                        telegram_user_id,
                        org_id,
                    )
                    await tg_message.reply_text(
                        f"Your Telegram account (ID: {telegram_user_id}) is not linked to an active user in this system. "
                        f"Please ask your administrator to set your Telegram ID ({telegram_user_id}) in your user profile."
                    )
                    return

                # ── 4. Rate limits ─────────────────────────────────────────
                async with aioredis.from_url(
                    settings.redis_url, decode_responses=False
                ) as redis_conn:
                    if not await check_org_telegram_rate(
                        redis_conn,
                        org_id,
                        daily_limit=settings.telegram_rate_limit_org_daily,
                    ):
                        await tg_message.reply_text(
                            "The organization has reached its daily message limit. "
                            "Please try again tomorrow."
                        )
                        return

                    if not await check_user_telegram_rate(
                        redis_conn,
                        user.id,
                        hourly_limit=settings.telegram_rate_limit_user_hourly,
                    ):
                        await tg_message.reply_text(
                            "You have reached your hourly message limit. "
                            "Please try again in a few minutes."
                        )
                        return

                # ── 5. Typing indicator ────────────────────────────────────
                from telegram.constants import ChatAction
                await context.bot.send_chat_action(
                    chat_id=tg_chat.id, action=ChatAction.TYPING
                )

                # ── 6. Get/create conversation ─────────────────────────────
                conversation, tenant_session, _is_new = await get_or_create_conversation(
                    session=session,
                    org_id=org_id,
                    user=user,
                    channel_chat_id=chat_id,
                    channel="telegram",
                )

                # ── 7. Persist inbound message ─────────────────────────────
                inbound_msg = GSageChannelMessage(
                    org_id=org_id,
                    user_id=user.id,
                    channel="telegram",
                    channel_chat_id=chat_id,
                    channel_message_id=channel_message_id,
                    direction=GSageChannelDirection.INBOUND,
                    status=GSageChannelStatus.PROCESSING,
                    text=text,
                    session_id=tenant_session.id,
                )
                session.add(inbound_msg)
                await session.flush()  # assign PK before commit
                inbound_msg_id = inbound_msg.id  # save for cross-phase access

                # ── 8. Load org + membership ───────────────────────────────
                org = await session.get(GSageOrganization, org_id)

                membership_result = await session.execute(
                    select(GSageUserOrganization).where(
                        GSageUserOrganization.user_id == user.id,
                        GSageUserOrganization.org_id == org_id,
                    )
                )
                membership = membership_result.scalars().first()
                if membership is None:
                    logger.error(
                        "handle_message: no membership — user_id=%s org_id=%s",
                        user.id,
                        org_id,
                    )
                    inbound_msg.status = GSageChannelStatus.FAILED
                    inbound_msg.error_message = "User has no membership in this organization"
                    await tg_message.reply_text("Access error: user membership not found.")
                    return

                # ── 9. Resolve user's default department ──────────────────
                from src.shared.models.department import GSageDepartment
                from src.shared.models.user_department import GSageUserDepartment

                dept_result = await session.execute(
                    select(GSageDepartment)
                    .join(
                        GSageUserDepartment,
                        GSageUserDepartment.dept_id == GSageDepartment.id,
                    )
                    .where(
                        GSageUserDepartment.user_id == user.id,
                        GSageUserDepartment.is_active.is_(True),
                        GSageDepartment.org_id == org_id,
                        GSageDepartment.is_active.is_(True),
                    )
                    .order_by(
                        GSageDepartment.is_default.desc(),
                    )
                )
                tg_dept = dept_result.scalars().first()
                tg_dept_id = tg_dept.id if tg_dept else None

                # ── 10. Build TenantContext + agent ────────────────────────
                ctx = TenantContext(
                    user_id=user.id,
                    org_id=org_id,
                    org_role=membership.role,
                    permissions=permissions_for_role(membership.role),
                    email=getattr(user, "email", None),
                    interface="telegram",
                    dept_id=tg_dept_id,
                )

                # Use the agno_session_id stored by conversation_manager so
                # the persist_agno_run_projection post-hook can find this
                # session and route the response to the correct channel.
                session_id = tenant_session.agno_session_id
                tenant_session_id = tenant_session.id
                conversation_id = conversation.id

                tg_profile_org, tg_profile_user = await load_interface_profiles(
                    org_id, user.id, "telegram", session
                )
                agent = build_agent(
                    ctx=ctx,
                    agent_id="assistant",
                    session_id=session_id,
                    org=org,
                    user=user,
                    interface_profile_org=tg_profile_org,
                    interface_profile_user=tg_profile_user,
                    gsage_session_id=tenant_session_id,
                )
            # ── Phase 1 ends: session.begin() commits here ─────────────────
            # GSageTenantSession row is now visible to all DB connections.

        # ═══════════════════════════════════════════════════════════════════════
        # PHASE 2 — no active transaction: run agent
        # Agno's post-hook opens its own DB connection and can now see the
        # committed GSageTenantSession row, so no deadlock occurs.
        # ═══════════════════════════════════════════════════════════════════════

        # Load pending background notifications + dept name in a short read session.
        async with AsyncSession_() as read_session:
            pending_bg_tasks = await get_pending_bg_notifications(
                tenant_session_id, read_session
            )
            dept_name = (
                await load_dept_name(ctx.dept_id, read_session)
                if ctx.dept_id is not None
                else None
            )

        effective_text = text
        if pending_bg_tasks:
            bg_block = build_bg_context_block(pending_bg_tasks)
            effective_text = f"{bg_block}\n\n---\n{effective_text}"

        if ctx.dept_id is not None:
            dept_block = build_dept_context_block(ctx.dept_id, dept_name)
            effective_text = f"{dept_block}\n\n---\n{effective_text}"

        import asyncio
        from agno.run import RunStatus

        # Retry loop for transient LLM provider errors (e.g. HTTP 503 / UNAVAILABLE).
        # Agno swallows these errors and returns RunOutput with status=RunStatus.error
        # instead of raising, so we check the status explicitly after each attempt.
        _MAX_AGENT_RETRIES = 2
        run_output = None
        for _attempt in range(_MAX_AGENT_RETRIES + 1):
            run_output = await agent.arun(effective_text)
            if getattr(run_output, "status", None) != RunStatus.error:
                break
            if _attempt < _MAX_AGENT_RETRIES:
                _delay = 2.0 * (2 ** _attempt)
                logger.warning(
                    "handle_message: agent run error (attempt %d/%d), "
                    "retrying in %.0fs — tg_id=%s error=%s",
                    _attempt + 1, _MAX_AGENT_RETRIES + 1, _delay,
                    telegram_user_id,
                    getattr(run_output, "content", ""),
                )
                await asyncio.sleep(_delay)

        if getattr(run_output, "status", None) == RunStatus.error:
            logger.error(
                "handle_message: agent run failed after %d attempts — tg_id=%s error=%s",
                _MAX_AGENT_RETRIES + 1,
                telegram_user_id,
                getattr(run_output, "content", ""),
            )
            raise RuntimeError("LLM provider temporarily unavailable. Please try again.")

        # ═══════════════════════════════════════════════════════════════════════
        # PHASE 3 — short transaction: persist results
        # Re-fetches inbound_msg and conversation by ID since Phase 1 session
        # is already closed.
        # ═══════════════════════════════════════════════════════════════════════
        async with AsyncSession_() as fin_session:
            async with fin_session.begin():
                inbound_msg = await fin_session.get(GSageChannelMessage, inbound_msg_id)
                conversation = await fin_session.get(GSageChannelConversation, conversation_id)

                if pending_bg_tasks:
                    await mark_bg_tasks_notified(
                        [t.id for t in pending_bg_tasks], fin_session
                    )

                # ── 11b. HITL: check if run is paused for approval ─────────
                if getattr(run_output, "status", None) == RunStatus.paused:
                    from src.backend_api.app.services.approval_delegations import (
                        extract_approval_ids_from_run_output,
                        process_approval_delegations,
                    )
                    pending_approval_ids = extract_approval_ids_from_run_output(run_output)
                    if pending_approval_ids:
                        await process_approval_delegations(
                            approval_ids=pending_approval_ids,
                            ctx=ctx,
                            db=fin_session,
                            org=org,
                            agno_session_id=session_id,
                            run_id=str(getattr(run_output, "run_id", "") or ""),
                        )

                    paused_content = getattr(run_output, "content", None)
                    response_text = str(paused_content) if paused_content else ""
                    if not response_text.strip():
                        response_text = (
                            "This action requires human approval before it can be executed. "
                            "You will receive the result here once it's approved."
                        )

                    max_len = settings.telegram_max_message_length or _DEFAULT_MAX_LEN
                    response_text = _markdown_to_telegram_html(response_text)
                    chunks = _split_text(response_text, max_len)
                    for chunk in chunks:
                        await tg_message.reply_text(chunk, parse_mode="HTML")

                    outbound_msg = GSageChannelMessage(
                        org_id=org_id,
                        user_id=user.id,
                        channel="telegram",
                        channel_chat_id=chat_id,
                        channel_message_id=f"reply_{channel_message_id}",
                        direction=GSageChannelDirection.OUTBOUND,
                        status=GSageChannelStatus.COMPLETED,
                        text=response_text,
                        session_id=tenant_session_id,
                    )
                    fin_session.add(outbound_msg)
                    if inbound_msg:
                        inbound_msg.status = GSageChannelStatus.COMPLETED
                    if conversation:
                        conversation.message_count = (conversation.message_count or 0) + 2
                    return  # fin_session.begin() commits on exit — continuation via Celery after approval

                response_text: str = ""
                if run_output is not None:
                    content = getattr(run_output, "content", None)
                    response_text = str(content) if content else str(run_output)

                if not response_text.strip():
                    raise ValueError("Agent returned an empty response")

                # ── 12. Reply (split into chunks) ──────────────────────────
                max_len = settings.telegram_max_message_length or _DEFAULT_MAX_LEN
                response_text = _markdown_to_telegram_html(response_text)
                chunks = _split_text(response_text, max_len)
                for chunk in chunks:
                    await tg_message.reply_text(chunk, parse_mode="HTML")

                # ── 13. Persist outbound + mark inbound completed ──────────
                outbound_msg = GSageChannelMessage(
                    org_id=org_id,
                    user_id=user.id,
                    channel="telegram",
                    channel_chat_id=chat_id,
                    channel_message_id=f"reply_{channel_message_id}",
                    direction=GSageChannelDirection.OUTBOUND,
                    status=GSageChannelStatus.COMPLETED,
                    text=response_text,
                    session_id=tenant_session_id,
                )
                fin_session.add(outbound_msg)

                if inbound_msg:
                    inbound_msg.status = GSageChannelStatus.COMPLETED
                if conversation:
                    conversation.message_count = (conversation.message_count or 0) + 2
                # fin_session.begin() commits on context manager exit

    except Exception as exc:
        logger.exception(
            "handle_message: unhandled error — tg_id=%s chat_id=%s error=%s",
            telegram_user_id,
            chat_id,
            exc,
        )
        # Best-effort: mark message as failed and notify user.
        try:
            if inbound_msg_id is not None:
                async with AsyncSession_() as err_session:
                    async with err_session.begin():
                        refreshed = await err_session.get(GSageChannelMessage, inbound_msg_id)
                        if refreshed:
                            refreshed.status = GSageChannelStatus.FAILED
                            refreshed.error_message = str(exc)[:1000]
        except Exception:
            pass
        try:
            await tg_message.reply_text(
                "An internal error occurred while processing your message. "
                "Please try again later or contact your administrator."
            )
        except Exception:
            pass
    finally:
        # Cleanup MCP sessions BEFORE disposing the engine so the anyio
        # cancel scope inside the MCP transport can unwind cleanly.
        # Without this, a zombie httpx connection (CLOSE-WAIT on our
        # side) pins the event loop at 100% CPU via _deliver_cancellation.
        if agent is not None:
            try:
                from src.shared.services.mcp_cleanup import cleanup_agent_mcp

                await cleanup_agent_mcp(agent)
            except Exception:
                logger.debug("MCP cleanup failed (ignored)", exc_info=True)
        await engine.dispose()
