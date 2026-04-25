"""gSage AI — Celery task: process inbound email.

Pipeline (per PROMPT.md Phase 7):
  1. Load GSageEmailMessage from DB (idempotency: skip if COMPLETED).
  2. Mark PROCESSING.
  3. Org-level daily rate-limit check (Redis).
  4. Resolve sender → GSageUser.
  5. Load membership (GSageUserOrganization) to obtain role.
  6. Build TenantContext.
  7. Reconstruct ParsedEmail from stored fields; get/create thread+conversation.
  8. User-level hourly new-thread rate-limit check (new threads only).
  9. Build Agno agent (build_agent) and run with email body.
  10. Send SMTP reply via smtp_sender.send_reply.
  11. Persist outbound GSageEmailMessage.
  12. Mark inbound message COMPLETED.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from src.backend_api.app.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(
    name="src.backend_api.app.tasks.email.process_email_inbound",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def process_email_inbound(self, *, message_id: str, org_id: str) -> dict:  # type: ignore[return]
    """Process a single inbound email through the AI agent pipeline.

    Dispatched by the email worker immediately after the raw message is
    persisted to the database.

    Args:
        message_id: IMAP Message-ID header value (globally unique).
        org_id:     Organization UUID as string.
    """
    try:
        return asyncio.run(
            _run_with_cleanup(
                message_id=message_id,
                org_id=uuid.UUID(org_id),
                retry_attempt=self.request.retries,
            )
        )
    except Exception as exc:
        log.error(
            "process_email_inbound: unhandled error — message_id=%s error=%s",
            message_id,
            exc,
            exc_info=True,
        )
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# Async implementation
# ---------------------------------------------------------------------------


def _build_retry_notice_block(attempt: int) -> str:
    """Build a [SYSTEM_NOTICE] block injected on Celery retry attempts.

    Informs the agent that the previous run failed due to a transient error
    and that any tool calls may have produced errors or partial results.
    """
    return (
        "[SYSTEM_NOTICE]\n"
        f"This is retry attempt {attempt} of this request. "
        "The previous attempt failed due to a transient error (e.g. LLM provider "
        "unavailability). Any tools invoked during the previous attempt may have "
        "returned errors or incomplete results. Please retry any tool calls that "
        "are needed and do not assume results from the previous attempt are valid.\n"
        "[/SYSTEM_NOTICE]"
    )


async def _run_with_cleanup(
    *, message_id: str, org_id: uuid.UUID, retry_attempt: int = 0
) -> dict:
    """Thin wrapper that disposes DB pools before the event loop closes.

    Celery's ForkPoolWorker closes the asyncio event loop after each
    ``asyncio.run()`` call.  Without explicit disposal, asyncpg connections
    held in module-level pools survive past the loop shutdown and trigger
    noisy "Event loop is closed" / "Future attached to a different loop"
    errors during GC.
    """
    try:
        return await _process_async(
            message_id=message_id, org_id=org_id, retry_attempt=retry_attempt
        )
    finally:
        from src.shared.database import dispose_engine_pool
        from src.backend_api.app.services.agent_factory import dispose_agno_db_pool

        await dispose_engine_pool()
        await dispose_agno_db_pool()


async def _process_async(
    *, message_id: str, org_id: uuid.UUID, retry_attempt: int = 0
) -> dict:
    """Async email processing pipeline."""
    import redis.asyncio as aioredis
    from sqlalchemy import select

    from src.shared.config.settings import get_settings
    from src.shared.database import _get_session_maker
    from src.shared.models.email_account import GSageEmailAccount
    from src.shared.models.email_message import (
        GSageEmailDirection,
        GSageEmailMessage,
        GSageEmailStatus,
    )
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.user_organization import GSageUserOrganization
    from src.backend_api.app.core.tenant import TenantContext, permissions_for_role
    from src.backend_api.app.services.agent_factory import build_agent, load_interface_profiles
    from src.email_worker.parser import ParsedEmail, normalize_subject
    from src.email_worker.rate_limiter import check_org_email_rate, check_user_thread_rate
    from src.email_worker.resolver import resolve_sender
    from src.email_worker.smtp_sender import send_reply
    from src.email_worker.thread_manager import get_or_create_thread

    settings = get_settings()
    session_maker = _get_session_maker()

    async with session_maker() as session:
        # -- 1. Load message (tenant-scoped) -----------------------------------
        result = await session.execute(
            select(GSageEmailMessage).where(
                GSageEmailMessage.message_id == message_id,
                GSageEmailMessage.org_id == org_id,
            )
        )
        msg = result.scalars().first()

        if msg is None:
            log.error(
                "process_email_inbound: message not found — message_id=%s org_id=%s",
                message_id,
                org_id,
            )
            return {"status": "error", "detail": "message not found"}

        if msg.status == GSageEmailStatus.COMPLETED:
            log.info(
                "process_email_inbound: already completed — message_id=%s",
                message_id,
            )
            return {"status": "skipped"}

        # -- 2. Transition to PROCESSING (idempotency guard) -------------------
        msg.status = GSageEmailStatus.PROCESSING
        await session.commit()

        # Track agent so we can run MCP cleanup on ALL exit paths below
        # (prevents anyio _deliver_cancellation busy-loop at 100% CPU).
        agent = None

        try:
            # -- 3. Load email account -----------------------------------------
            account = await session.get(GSageEmailAccount, msg.email_account_id)
            if account is None:
                raise ValueError(f"Email account {msg.email_account_id} not found")

            # -- 4. Rate limit: org-level daily emails -------------------------
            redis_conn = aioredis.from_url(
                settings.celery_broker_url,
                encoding="utf-8",
                decode_responses=True,
            )
            async with redis_conn:
                if not await check_org_email_rate(redis_conn, org_id):
                    msg.status = GSageEmailStatus.FAILED
                    msg.error_message = "Organization daily email rate limit exceeded"
                    await session.commit()
                    log.warning(
                        "process_email_inbound: org rate limit exceeded — org_id=%s",
                        org_id,
                    )
                    try:
                        await send_reply(
                            account,
                            to_addr=msg.from_addr,
                            subject=msg.subject,
                            body_text=(
                                "Your message could not be processed because the organization "
                                "has reached its daily email limit. Please try again tomorrow."
                            ),
                            in_reply_to=msg.message_id,
                            references=msg.references,
                        )
                    except Exception:
                        log.warning(
                            "process_email_inbound: failed to send org rate-limit reply — org_id=%s",
                            org_id,
                            exc_info=True,
                        )
                    return {"status": "rate_limited", "detail": "org daily limit"}

                # -- 5. Resolve sender -----------------------------------------
                user = await resolve_sender(session, msg.from_addr, org_id)
                if user is None:
                    msg.status = GSageEmailStatus.FAILED
                    msg.error_message = f"Unknown sender: {msg.from_addr}"
                    await session.commit()
                    log.warning(
                        "process_email_inbound: unknown sender — from=%s org_id=%s",
                        msg.from_addr,
                        org_id,
                    )
                    return {"status": "failed", "detail": "unknown sender"}

                # -- 6. Load membership (role for TenantContext) ---------------
                membership_result = await session.execute(
                    select(GSageUserOrganization).where(
                        GSageUserOrganization.user_id == user.id,
                        GSageUserOrganization.org_id == org_id,
                    )
                )
                membership = membership_result.scalars().first()
                if membership is None:
                    msg.status = GSageEmailStatus.FAILED
                    msg.error_message = "User has no membership in this organization"
                    await session.commit()
                    log.error(
                        "process_email_inbound: no membership — user_id=%s org_id=%s",
                        user.id,
                        org_id,
                    )
                    return {"status": "failed", "detail": "no membership"}

                # -- 7. Build TenantContext ------------------------------------
                # -- 7b. Resolve department (account-scoped, user-default fallback) --
                dept_id = account.dept_id
                if dept_id is None:
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
                        .order_by(GSageDepartment.is_default.desc())
                    )
                    email_dept = dept_result.scalars().first()
                    dept_id = email_dept.id if email_dept else None

                ctx = TenantContext(
                    user_id=user.id,
                    org_id=org_id,
                    org_role=membership.role,
                    permissions=permissions_for_role(membership.role),
                    email=getattr(user, "email", None),
                    interface="email",
                    dept_id=dept_id,
                )

                # -- 8. Reconstruct ParsedEmail and get/create thread ----------
                parsed = ParsedEmail(
                    message_id=msg.message_id,
                    in_reply_to=msg.in_reply_to,
                    references=msg.references.split() if msg.references else [],
                    from_addr=msg.from_addr,
                    to_addr=msg.to_addr,
                    subject=msg.subject,
                    normalized_subject=normalize_subject(msg.subject),
                    date=msg.created_at,
                    body_text=msg.body_text or "",
                    body_html=msg.body_html,
                    raw_size_bytes=len((msg.body_text or "").encode()),
                )
                thread, conversation, is_new_thread = await get_or_create_thread(
                    session=session,
                    parsed=parsed,
                    user=user,
                    account=account,
                )

                # -- 9. Rate limit: user-level new threads per hour -------------
                if is_new_thread and not await check_user_thread_rate(
                    redis_conn, user.id
                ):
                    # Roll back the orphan thread and session created above
                    # before committing, so they don't persist in the DB.
                    await session.delete(conversation)
                    await session.delete(thread)
                    msg.status = GSageEmailStatus.FAILED
                    msg.error_message = "User hourly new-thread rate limit exceeded"
                    await session.commit()
                    log.warning(
                        "process_email_inbound: user thread rate limit — user_id=%s",
                        user.id,
                    )
                    try:
                        await send_reply(
                            account,
                            to_addr=msg.from_addr,
                            subject=msg.subject,
                            body_text=(
                                "Your message could not be processed because you have reached "
                                "your hourly limit for new conversations. "
                                "Please try again in a few minutes."
                            ),
                            in_reply_to=msg.message_id,
                            references=msg.references,
                        )
                    except Exception:
                        log.warning(
                            "process_email_inbound: failed to send user rate-limit reply — user_id=%s",
                            user.id,
                            exc_info=True,
                        )
                    return {"status": "rate_limited", "detail": "user thread limit"}

            # Redis connection closed — no longer needed.

            # -- 10. Link message to thread + session -------------------------
            msg.thread_id = thread.id
            msg.session_id = conversation.id
            msg.user_id = user.id

            # -- 11. Build deterministic Agno session ID ----------------------
            session_id = ctx.build_session_id("email-conv", str(conversation.id))

            # CRITICAL: we MUST commit here before calling agent.arun().
            #
            # Agno's persist_agno_run_projection post-hook opens a separate
            # DB connection to upsert gsage_tenant_sessions by
            # agno_session_id.  If this outer transaction is still open with
            # the flushed (but uncommitted) GSageTenantSession row, the
            # post-hook's INSERT blocks on the UNIQUE constraint of
            # agno_session_id — causing a deadlock.  Postgres eventually
            # terminates our connection, rolling back EVERYTHING flushed in
            # this transaction (thread, session, message updates).  The
            # outbound email then never gets sent.
            #
            # Committing here makes the thread + tenant_session visible to
            # the post-hook's separate connection so it can find and reuse
            # them instead of creating a duplicate row.
            if not conversation.agno_session_id:
                conversation.agno_session_id = session_id
            await session.commit()

            # -- 12. Load org for per-org LLM settings ------------------------
            org = await session.get(GSageOrganization, org_id)

            # -- 13. Build and run agent --------------------------------------
            from src.backend_api.app.services.background_tasks import (
                build_bg_context_block,
                build_dept_context_block,
                get_pending_bg_notifications,
                load_dept_name,
                mark_bg_tasks_notified,
            )

            email_profile_org, email_profile_user = await load_interface_profiles(
                org_id, user.id, "email", session
            )
            agent = build_agent(
                ctx=ctx,
                agent_id="assistant",
                session_id=session_id,
                org=org,
                user=user,
                source="email",
                interface_profile_org=email_profile_org,
                interface_profile_user=email_profile_user,
                gsage_session_id=conversation.id,
            )

            # Inject completed background task results and department context
            pending_bg_tasks = await get_pending_bg_notifications(
                conversation.id, session
            )
            effective_text = msg.body_text or msg.subject or "(no content)"
            if pending_bg_tasks:
                bg_block = build_bg_context_block(pending_bg_tasks)
                effective_text = f"{bg_block}\n\n---\n{effective_text}"
            if ctx.dept_id is not None:
                dept_name = await load_dept_name(ctx.dept_id, session)
                dept_block = build_dept_context_block(ctx.dept_id, dept_name)
                effective_text = f"{dept_block}\n\n---\n{effective_text}"

            if retry_attempt > 0:
                retry_block = _build_retry_notice_block(retry_attempt)
                effective_text = f"{retry_block}\n\n---\n{effective_text}"

            run_output = await agent.arun(effective_text)

            if pending_bg_tasks:
                await mark_bg_tasks_notified(
                    [t.id for t in pending_bg_tasks], session
                )

            # -- 13b. HITL: check if run is paused for approval ---------------
            from agno.run import RunStatus

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
                        db=session,
                        org=org,
                        agno_session_id=session_id,
                        run_id=str(getattr(run_output, "run_id", "") or ""),
                    )

                # Always send a neutral approval-pending message.
                # The LLM content on a paused run may be a partial or
                # misleading response (e.g. "Let me check and I already sent…").
                # The real reply will arrive once the action is approved.
                paused_response = (
                    "This action requires human approval before it can be executed. "
                    "You will receive the result via email once it has been approved."
                )

                paused_msg_id = await send_reply(
                    account=account,
                    to_addr=msg.from_addr,
                    subject=msg.subject,
                    body_text=paused_response,
                    in_reply_to=msg.message_id,
                    references=msg.references,
                )

                # Persist the outbound approval-notification message and mark
                # the inbound as complete (the real reply will come after
                # human approval — no second email should be sent here).
                approval_notif = GSageEmailMessage(
                    org_id=org_id,
                    user_id=user.id,
                    email_account_id=msg.email_account_id,
                    message_id=paused_msg_id,
                    in_reply_to=msg.message_id,
                    references=(
                        ((msg.references or "").strip() + " " + msg.message_id).strip()
                    ),
                    direction=GSageEmailDirection.OUTBOUND,
                    status=GSageEmailStatus.COMPLETED,
                    from_addr=msg.to_addr,
                    to_addr=msg.from_addr,
                    subject=msg.subject,
                    body_text=paused_response,
                    thread_id=thread.id,
                    session_id=conversation.id,
                )
                session.add(approval_notif)
                msg.status = GSageEmailStatus.COMPLETED
                await session.commit()
                log.info(
                    "process_email_inbound: paused for approval — message_id=%s",
                    message_id,
                )
                return {"status": "paused", "outbound_message_id": paused_msg_id}

            # -- 13c. Check for provider error (Agno returns RunStatus.error
            # instead of raising for transient failures like HTTP 503) -------
            if getattr(run_output, "status", None) == RunStatus.error:
                raise RuntimeError(
                    "Agent run failed (LLM provider error): "
                    f"{getattr(run_output, 'content', '')}"
                )

            # -- 14. Extract response text ------------------------------------
            response_text: str = ""
            if run_output is not None:
                content = getattr(run_output, "content", None)
                response_text = str(content) if content else str(run_output)

            if not response_text.strip():
                raise ValueError("Agent returned an empty response")

            # -- 15. Send SMTP reply ------------------------------------------
            outbound_msg_id = await send_reply(
                account=account,
                to_addr=msg.from_addr,
                subject=msg.subject,
                body_text=response_text,
                in_reply_to=msg.message_id,
                references=msg.references,
            )

            # -- 16. Persist outbound email record ----------------------------
            outbound = GSageEmailMessage(
                org_id=org_id,
                user_id=user.id,
                email_account_id=msg.email_account_id,
                message_id=outbound_msg_id,
                in_reply_to=msg.message_id,
                references=(
                    ((msg.references or "").strip() + " " + msg.message_id).strip()
                ),
                direction=GSageEmailDirection.OUTBOUND,
                status=GSageEmailStatus.COMPLETED,
                from_addr=msg.to_addr,
                to_addr=msg.from_addr,
                subject=msg.subject,
                body_text=response_text,
                thread_id=thread.id,
                session_id=conversation.id,
            )
            session.add(outbound)

            # -- 17. Mark inbound as completed --------------------------------
            msg.status = GSageEmailStatus.COMPLETED
            await session.commit()

            log.info(
                "process_email_inbound: completed — message_id=%s outbound_id=%s",
                message_id,
                outbound_msg_id,
            )
            return {"status": "ok", "outbound_message_id": outbound_msg_id}

        except Exception as exc:
            log.error(
                "process_email_inbound: pipeline failed — message_id=%s error=%s",
                message_id,
                exc,
                exc_info=True,
            )
            try:
                msg.status = GSageEmailStatus.FAILED
                msg.error_message = str(exc)[:500]
                await session.commit()
            except Exception:
                pass
            raise
        finally:
            # Cleanup MCP sessions so the anyio cancel scope unwinds cleanly.
            # Without this a zombie httpx connection pins the event loop at
            # 100% CPU via _deliver_cancellation.
            if agent is not None:
                try:
                    from src.shared.services.mcp_cleanup import cleanup_agent_mcp

                    await cleanup_agent_mcp(agent)
                except Exception:
                    log.debug("MCP cleanup failed (ignored)", exc_info=True)
