"""gSage AI — Microsoft Teams turn handler.

Pipeline executed for each inbound Teams activity (after the FastAPI
router has authenticated the JWT and resolved the
``GSageInterfaceProfile``):

  1. Extract AAD Object ID, conversation ID, and text from the activity.
  2. Strip the ``@bot`` mention left by Teams.
  3. Resolve sender → ``GSageUser`` (DB direct, then Graph fallback).
  4. Org/user rate-limit (Redis).
  5. Get/create ``GSageChannelConversation`` + ``GSageTenantSession``.
     Persist the Bot Framework ``ConversationReference`` for proactive
     outbound.
  6. Persist inbound ``GSageChannelMessage`` (PROCESSING).
  7. Build ``TenantContext`` → ``build_agent`` → ``agent.arun``.
  8. Send the markdown reply back via ``turn_context.send_activity``;
     persist outbound + mark inbound COMPLETED.

The function is invoked from inside ``BotFrameworkAdapter.process_activity``
so any unhandled exception will be surfaced by the adapter.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)


async def handle_teams_turn(
    *,
    profile: Any,
    turn_context: Any,
    graph_client: Optional[Any] = None,
    redis_client: Optional[Any] = None,
) -> None:
    """Process one Teams turn end-to-end.

    Args:
        profile:       The active ``GSageInterfaceProfile`` (interface=teams)
                       owning this Bot Framework App ID.
        turn_context:  ``botbuilder.core.TurnContext`` wrapping the activity.
        graph_client:  Optional ``GraphClient`` for first-contact email
                       resolution.
        redis_client:  Optional ``redis.asyncio`` client. When omitted, a
                       short-lived one is created from settings.
    """
    # Imports kept local to avoid pulling botbuilder + the worker stack
    # into modules that don't need them.
    from botbuilder.core import MessageFactory, TurnContext
    from botbuilder.schema import ConversationReference

    from src.backend_api.app.core.tenant import TenantContext, permissions_for_role
    from src.backend_api.app.services.agent_factory import (
        build_agent,
        load_interface_profiles,
    )
    from src.backend_api.app.services.background_tasks import (
        build_bg_context_block,
        build_dept_context_block,
        get_pending_bg_notifications,
        load_dept_name,
        mark_bg_tasks_notified,
    )
    from src.shared.config.settings import get_settings
    from src.shared.models.channel_conversation import GSageChannelConversation
    from src.shared.models.channel_message import (
        GSageChannelDirection,
        GSageChannelMessage,
        GSageChannelStatus,
    )
    from src.shared.models.department import GSageDepartment
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.user_department import GSageUserDepartment
    from src.shared.models.user_organization import GSageUserOrganization
    from src.teams_handler.conversation_manager import get_or_create_conversation
    from src.teams_handler.formatting import (
        DEFAULT_MAX_LEN as _DEFAULT_MAX_LEN,
        split_text as _split_text,
        strip_bot_mention,
    )
    from src.teams_handler.rate_limiter import (
        check_org_teams_rate,
        check_user_teams_rate,
    )
    from src.teams_handler.resolver import resolve_teams_sender

    activity = turn_context.activity
    if not activity:
        return
    if (getattr(activity, "type", "") or "").lower() != "message":
        # Ignore conversationUpdate / typing / event / invoke for now.
        return

    text = strip_bot_mention(activity)
    if not text:
        return

    sender = getattr(activity, "from_property", None) or getattr(
        activity, "from", None
    )
    aad_object_id = getattr(sender, "aad_object_id", None) if sender else None
    sender_name = getattr(sender, "name", None) if sender else None
    if not aad_object_id:
        sender_id = getattr(sender, "id", None) if sender else None
        sender_role = getattr(sender, "role", None) if sender else None
        channel_data = getattr(activity, "channel_data", None) or {}
        tenant_info = (
            channel_data.get("tenant")
            if isinstance(channel_data, dict)
            else None
        ) or {}
        cd_tenant_id = (
            tenant_info.get("id") if isinstance(tenant_info, dict) else None
        )
        conv_obj_dbg = getattr(activity, "conversation", None)
        conv_id_dbg = (
            getattr(conv_obj_dbg, "id", None)
            if conv_obj_dbg is not None
            else None
        )
        conv_type_dbg = (
            getattr(conv_obj_dbg, "conversation_type", None)
            if conv_obj_dbg is not None
            else None
        )
        logger.warning(
            "handle_teams_turn: activity without aadObjectId — "
            "profile_id=%s sender_id=%s sender_name=%s sender_role=%s "
            "channel_tenant_id=%s conversation_id=%s conversation_type=%s "
            "service_url=%s",
            profile.id,
            sender_id,
            sender_name,
            sender_role,
            cd_tenant_id,
            conv_id_dbg,
            conv_type_dbg,
            getattr(activity, "service_url", None),
        )
        await turn_context.send_activity(
            MessageFactory.text(
                "Sorry, I cannot identify your Microsoft 365 account from this "
                "Teams message. Please contact your administrator."
            )
        )
        return

    conv_obj = getattr(activity, "conversation", None)
    channel_chat_id = (
        getattr(conv_obj, "id", None) if conv_obj is not None else None
    ) or ""
    activity_id = getattr(activity, "id", None) or str(uuid.uuid4())

    # Snapshot the ConversationReference once — used both to persist for
    # proactive outbound and to log diagnostics on failure.
    conv_ref_obj = TurnContext.get_conversation_reference(activity)
    try:
        conv_ref_dict = conv_ref_obj.serialize()
    except Exception:
        # botbuilder ≥ 4.15 exposes `as_dict` instead of `serialize`.
        conv_ref_dict = (
            conv_ref_obj.as_dict() if hasattr(conv_ref_obj, "as_dict") else None
        )

    org_id = profile.org_id
    profile_id = profile.id

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    AsyncSession_ = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    inbound_msg_id: Optional[uuid.UUID] = None
    agent: Any = None
    redis_owned = False

    try:
        if redis_client is None:
            import redis.asyncio as aioredis

            redis_client = aioredis.from_url(
                settings.redis_url, decode_responses=False
            )
            redis_owned = True

        # ═════════ PHASE 1 — short transaction ═════════════════════════
        async with AsyncSession_() as session:
            async with session.begin():
                user = await resolve_teams_sender(
                    session,
                    aad_object_id=str(aad_object_id),
                    org_id=org_id,
                    graph=graph_client,
                )
                if user is None:
                    # Collect every identifying field the Bot Framework gave us
                    # so administrators can correlate the inbound message with
                    # an Entra account when registering / linking the user.
                    sender_id = getattr(sender, "id", None) if sender else None
                    sender_role = (
                        getattr(sender, "role", None) if sender else None
                    )
                    channel_data = getattr(activity, "channel_data", None) or {}
                    tenant_info = (
                        channel_data.get("tenant")
                        if isinstance(channel_data, dict)
                        else None
                    ) or {}
                    cd_tenant_id = (
                        tenant_info.get("id")
                        if isinstance(tenant_info, dict)
                        else None
                    )
                    conv_type = (
                        getattr(conv_obj, "conversation_type", None)
                        if conv_obj is not None
                        else None
                    )
                    service_url = getattr(activity, "service_url", None)
                    locale = getattr(activity, "locale", None)
                    logger.warning(
                        "handle_teams_turn: unknown sender — "
                        "org_id=%s profile_id=%s aad_object_id=%s "
                        "sender_id=%s sender_name=%s sender_role=%s "
                        "channel_tenant_id=%s conversation_id=%s "
                        "conversation_type=%s service_url=%s locale=%s",
                        org_id,
                        profile_id,
                        aad_object_id,
                        sender_id,
                        sender_name,
                        sender_role,
                        cd_tenant_id,
                        channel_chat_id,
                        conv_type,
                        service_url,
                        locale,
                    )
                    await turn_context.send_activity(
                        MessageFactory.text(
                            "Your Microsoft 365 account is not linked to an "
                            "active user in this system. Please ask your "
                            "administrator to register your account."
                        )
                    )
                    return

                if not await check_org_teams_rate(
                    redis_client,
                    org_id,
                    daily_limit=settings.teams_rate_limit_org_daily,
                ):
                    await turn_context.send_activity(
                        MessageFactory.text(
                            "The organization has reached its daily message "
                            "limit. Please try again tomorrow."
                        )
                    )
                    return
                if not await check_user_teams_rate(
                    redis_client,
                    user.id,
                    hourly_limit=settings.teams_rate_limit_user_hourly,
                ):
                    await turn_context.send_activity(
                        MessageFactory.text(
                            "You have reached your hourly message limit. "
                            "Please try again in a few minutes."
                        )
                    )
                    return

                conversation, tenant_session, _is_new = (
                    await get_or_create_conversation(
                        session,
                        org_id=org_id,
                        user=user,
                        channel_chat_id=channel_chat_id,
                        conversation_reference=conv_ref_dict,
                        profile_id=profile_id,
                    )
                )

                inbound_msg = GSageChannelMessage(
                    org_id=org_id,
                    user_id=user.id,
                    channel="teams",
                    channel_chat_id=channel_chat_id,
                    channel_message_id=str(activity_id),
                    direction=GSageChannelDirection.INBOUND,
                    status=GSageChannelStatus.PROCESSING,
                    text=text,
                    session_id=tenant_session.id,
                )
                session.add(inbound_msg)
                await session.flush()
                inbound_msg_id = inbound_msg.id

                org = await session.get(GSageOrganization, org_id)
                membership = (
                    await session.execute(
                        select(GSageUserOrganization).where(
                            GSageUserOrganization.user_id == user.id,
                            GSageUserOrganization.org_id == org_id,
                        )
                    )
                ).scalars().first()
                if membership is None:
                    inbound_msg.status = GSageChannelStatus.FAILED
                    inbound_msg.error_message = (
                        "User has no membership in this organization"
                    )
                    await turn_context.send_activity(
                        MessageFactory.text(
                            "Access error: user membership not found."
                        )
                    )
                    return

                # Default department lookup (mirror of telegram_worker).
                dept_row = (
                    await session.execute(
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
                        .order_by(GSageDepartment.is_default.desc())
                    )
                ).scalars().first()
                dept_id = dept_row.id if dept_row else None

                ctx = TenantContext(
                    user_id=user.id,
                    org_id=org_id,
                    org_role=membership.role,
                    permissions=permissions_for_role(membership.role),
                    email=getattr(user, "email", None),
                    interface="teams",
                    dept_id=dept_id,
                )
                session_id = tenant_session.agno_session_id
                tenant_session_id = tenant_session.id
                conversation_id = conversation.id

                profile_org, profile_user = await load_interface_profiles(
                    org_id, user.id, "teams", session
                )
                agent = build_agent(
                    ctx=ctx,
                    agent_id="assistant",
                    session_id=session_id,
                    org=org,
                    user=user,
                    interface_profile_org=profile_org,
                    interface_profile_user=profile_user,
                    gsage_session_id=tenant_session_id,
                )
            # Phase 1 commits here.

        # ═════════ PHASE 2 — agent run (no active transaction) ═════════
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
            effective_text = (
                f"{build_bg_context_block(pending_bg_tasks)}\n\n---\n{effective_text}"
            )
        if ctx.dept_id is not None:
            effective_text = (
                f"{build_dept_context_block(ctx.dept_id, dept_name)}\n\n---\n{effective_text}"
            )

        import asyncio
        from agno.run import RunStatus

        _MAX_AGENT_RETRIES = 2
        run_output = None
        for _attempt in range(_MAX_AGENT_RETRIES + 1):
            run_output = await agent.arun(effective_text)
            if getattr(run_output, "status", None) != RunStatus.error:
                break
            if _attempt < _MAX_AGENT_RETRIES:
                await asyncio.sleep(2.0 * (2 ** _attempt))

        if getattr(run_output, "status", None) == RunStatus.error:
            raise RuntimeError(
                "LLM provider temporarily unavailable. Please try again."
            )

        # ═════════ PHASE 3 — persist results + reply ═══════════════════
        async with AsyncSession_() as fin_session:
            async with fin_session.begin():
                inbound_msg = await fin_session.get(
                    GSageChannelMessage, inbound_msg_id
                )
                conversation = await fin_session.get(
                    GSageChannelConversation, conversation_id
                )

                if pending_bg_tasks:
                    await mark_bg_tasks_notified(
                        [t.id for t in pending_bg_tasks], fin_session
                    )

                # HITL paused-run handling (mirror of Telegram).
                if getattr(run_output, "status", None) == RunStatus.paused:
                    from src.backend_api.app.services.approval_delegations import (
                        extract_approval_ids_from_run_output,
                        process_approval_delegations,
                    )

                    pending_ids = extract_approval_ids_from_run_output(run_output)
                    if pending_ids:
                        await process_approval_delegations(
                            approval_ids=pending_ids,
                            ctx=ctx,
                            db=fin_session,
                            org=org,
                            agno_session_id=session_id,
                            run_id=str(getattr(run_output, "run_id", "") or ""),
                        )
                    paused = getattr(run_output, "content", None)
                    response_text = str(paused) if paused else ""
                    if not response_text.strip():
                        response_text = (
                            "This action requires human approval before it can be "
                            "executed. You will receive the result here once it's "
                            "approved."
                        )
                else:
                    content = getattr(run_output, "content", None) if run_output else None
                    response_text = (
                        str(content) if content else (str(run_output) if run_output else "")
                    )
                    if not response_text.strip():
                        raise ValueError("Agent returned an empty response")

                max_len = settings.teams_max_message_length or _DEFAULT_MAX_LEN
                for chunk in _split_text(response_text, max_len):
                    msg = MessageFactory.text(chunk)
                    # Teams renders Markdown natively when textFormat is set.
                    msg.text_format = "markdown"
                    await turn_context.send_activity(msg)

                outbound_msg = GSageChannelMessage(
                    org_id=org_id,
                    user_id=user.id,
                    channel="teams",
                    channel_chat_id=channel_chat_id,
                    channel_message_id=f"reply_{activity_id}",
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

    except Exception as exc:
        logger.exception(
            "handle_teams_turn: unhandled error — aad_id=%s chat_id=%s err=%s",
            aad_object_id,
            channel_chat_id,
            exc,
        )
        try:
            if inbound_msg_id is not None:
                async with AsyncSession_() as err_session:
                    async with err_session.begin():
                        refreshed = await err_session.get(
                            GSageChannelMessage, inbound_msg_id
                        )
                        if refreshed:
                            refreshed.status = GSageChannelStatus.FAILED
                            refreshed.error_message = str(exc)[:1000]
        except Exception:
            pass
        try:
            await turn_context.send_activity(
                MessageFactory.text(
                    "An internal error occurred while processing your message. "
                    "Please try again later or contact your administrator."
                )
            )
        except Exception:
            pass
    finally:
        if agent is not None:
            try:
                from src.shared.services.mcp_cleanup import cleanup_agent_mcp

                await cleanup_agent_mcp(agent)
            except Exception:
                logger.debug("MCP cleanup failed (ignored)", exc_info=True)
        if redis_owned and redis_client is not None:
            try:
                await redis_client.close()
            except Exception:
                pass
        await engine.dispose()
