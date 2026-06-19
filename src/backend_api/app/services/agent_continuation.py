"""Agent Continuation Service — core logic for resuming agent runs.

Provides two async entry points that can be called from Celery tasks:

* ``continue_after_bg_task(task_id, db)`` — re-run the agent after a
  background tool completes, injecting the result into the prompt.
* ``continue_after_approval(approval_id, db)`` — resume a paused run after
  an approval is resolved (approved or rejected).

Both functions return ``(session, response_text)`` so the caller (Celery task)
can dispatch delivery via :mod:`channel_sender`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.core.tenant import TenantContext, permissions_for_role
from src.backend_api.app.services.agent_factory import (
    DEFAULT_AGENT_ID,
    build_agent,
    get_agno_db,
    load_interface_profiles,
)
from src.backend_api.app.services.approval_delegations import (
    extract_approval_ids_from_run_output,
    process_approval_delegations,
)
from src.backend_api.app.services.background_tasks import (
    build_bg_context_block,
    get_pending_bg_notifications,
    mark_bg_tasks_notified,
)
from src.shared.models.approval_delegation import GSageApprovalDelegation
from src.shared.models.background_task import GSageBackgroundTask
from src.shared.models.organization import GSageOrganization
from src.shared.models.tenant_session import GSageTenantSession
from src.shared.models.user import GSageUser
from src.shared.models.user_organization import GSageUserOrganization

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sentinel: not a real error, just "nothing left to do"
# ---------------------------------------------------------------------------

class ContinuationSkipped(Exception):
    """Raised when a continuation task has nothing to do.

    Typical reasons:
    - Background task results were already injected into the conversation
      by a subsequent user message before the Celery continuation task ran.
    - Approval record has been deleted, superseded, or is no longer
      present in the Agno database.

    Celery tasks should catch this exception and skip retries.
    """


# ---------------------------------------------------------------------------
# Helper: classify continuation errors as transient vs. permanent
# ---------------------------------------------------------------------------

def _is_transient_continuation_error(text: str) -> bool:
    """Return True for transient provider errors that are safe to retry.

    Mirrors :func:`src.backend_api.app.api.v1.chat._is_transient_llm_error`
    so the SSE handler and the Celery continuation tasks share the same
    retry policy.
    """
    t = (text or "").lower()
    return (
        "503" in t
        or "502" in t
        or "504" in t
        or "service unavailable" in t
        or "unavailable" in t
        or "timeout" in t
        or "timed out" in t
        or "connection reset" in t
        or "connection refused" in t
    )


# ---------------------------------------------------------------------------
# Helper: extract text from RunOutput
# ---------------------------------------------------------------------------

def _extract_text(content) -> str:
    """Extract plain text from a RunOutput content field."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif hasattr(block, "text"):
                parts.append(str(block.text))
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# Helper: rebuild TenantContext from a session
# ---------------------------------------------------------------------------

async def _rebuild_tenant_context(
    session: GSageTenantSession,
    db: AsyncSession,
    *,
    override_user_id: Optional[uuid.UUID] = None,
    override_dept_id: Optional[uuid.UUID] = None,
    interface: str = "web",
) -> TenantContext:
    """Rebuild a TenantContext from a stored TenantSession row.

    ``override_dept_id`` takes precedence over ``session.dept_id`` — used by
    the approval continuation to restore the exact department context that was
    active when the tool call was originally requested.
    """
    user_id = override_user_id or session.user_id
    if user_id is None:
        raise ValueError(f"Session {session.id} has no user_id and no override provided")

    membership_result = await db.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == user_id,
            GSageUserOrganization.org_id == session.org_id,
            GSageUserOrganization.is_active == True,  # noqa: E712
        )
    )
    membership = membership_result.scalar_one_or_none()
    role = membership.role if membership else "member"

    dept_id = override_dept_id if override_dept_id is not None else session.dept_id

    return TenantContext(
        user_id=user_id,
        org_id=session.org_id,
        org_role=role,
        permissions=permissions_for_role(role),
        interface=interface,
        dept_id=dept_id,
    )


# ---------------------------------------------------------------------------
# Helper: build agent for a session
# ---------------------------------------------------------------------------

async def _build_agent_for_session(
    session: GSageTenantSession,
    ctx: TenantContext,
    db: AsyncSession,
    org: Optional[GSageOrganization] = None,
    user: Optional[GSageUser] = None,
    source: str = "continuation",
):
    """Build an agent from a stored session, reusing the existing agno_session_id."""
    profile_org, profile_user = await load_interface_profiles(
        ctx.org_id, ctx.user_id, ctx.interface, db
    )
    return build_agent(
        ctx=ctx,
        agent_id=DEFAULT_AGENT_ID,
        session_id=session.agno_session_id,
        org=org,
        user=user,
        interface_profile_org=profile_org,
        interface_profile_user=profile_user,
        gsage_session_id=session.id,
        source=source,
    )


async def _safe_mcp_cleanup(agent) -> None:
    """Best-effort MCP cleanup — never raises.

    Must be called after ``agent.arun()``/``acontinue_run()`` completes
    (or fails) to prevent the anyio cancel-scope busy-loop at 100% CPU.
    Safe even when the agent has no MCP tools.
    """
    try:
        from src.shared.services.mcp_cleanup import cleanup_agent_mcp

        await cleanup_agent_mcp(agent)
    except Exception:
        log.debug("MCP cleanup failed (ignored)", exc_info=True)


# ---------------------------------------------------------------------------
# Public: continue after background task completion
# ---------------------------------------------------------------------------

async def continue_after_bg_task(
    task_id: str,
    db: AsyncSession,
) -> tuple[GSageTenantSession, str]:
    """Re-run the agent after a background tool has completed.

    Returns:
        (session, response_text) — caller is responsible for delivery.

    Raises:
        ValueError: if the task or session is not found.
    """
    from src.shared.models.background_task import BackgroundTaskStatus

    result = await db.execute(
        select(GSageBackgroundTask).where(
            GSageBackgroundTask.id == uuid.UUID(task_id)
        )
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise ValueError(f"Background task {task_id} not found")

    if task.status not in (BackgroundTaskStatus.COMPLETED, BackgroundTaskStatus.FAILED):
        raise ValueError(
            f"Background task {task_id} is not in a terminal state (status={task.status})"
        )

    # Resolve the TenantSession
    session_result = await db.execute(
        select(GSageTenantSession).where(
            GSageTenantSession.id == task.gsage_session_id
        )
    )
    tenant_session = session_result.scalar_one_or_none()
    if tenant_session is None:
        raise ValueError(f"TenantSession {task.gsage_session_id} not found for bg task {task_id}")

    # Determine interface from session source
    interface = _source_to_interface(tenant_session.source)

    # Rebuild context
    ctx = await _rebuild_tenant_context(tenant_session, db, interface=interface)

    # Load org
    org = await db.get(GSageOrganization, tenant_session.org_id)

    # Load user
    user = await db.get(GSageUser, ctx.user_id) if ctx.user_id else None

    # Build agent (source=bg_task so agent-runs can be filtered by origin)
    agent = await _build_agent_for_session(
        tenant_session, ctx, db, org=org, user=user, source="bg_task"
    )

    # Get pending bg task notifications and build context block
    pending_bg_tasks = await get_pending_bg_notifications(tenant_session.id, db)
    if not pending_bg_tasks:
        # The results were already consumed by a subsequent user message that
        # triggered get_pending_bg_notifications() before this Celery task ran.
        # This is expected when tasks were queued while no worker was listening.
        log.info(
            "continue_after_bg_task: task=%s session=%s — results already notified via "
            "another path; skipping continuation",
            task_id, tenant_session.id,
        )
        raise ContinuationSkipped(
            f"Background results for session {tenant_session.id} already notified"
        )

    bg_block = build_bg_context_block(pending_bg_tasks)
    # The bg_block already contains [BACKGROUND_TASKS_COMPLETED]...[/…] tags.
    # In the normal chat flow (chat.py), the user's real message follows the
    # "---" separator and remains visible after stripping.  Here there is no
    # real user message, so we embed the instruction INSIDE the sentinel block
    # (before the closing tag) so list_messages() strips the entire "user"
    # message.  Only the assistant response will be visible to the end user.
    #
    # We replace the closing tag in bg_block with the instruction + closing tag.
    instruction = (
        "\nBackground tasks have completed. Summarize the results for the user "
        "in a clear, concise message in the same language the user has been "
        "using in this conversation. If any task failed, explain the error."
    )
    prompt = bg_block.replace(
        "[/BACKGROUND_TASKS_COMPLETED]",
        f"{instruction}\n[/BACKGROUND_TASKS_COMPLETED]\n\n---\n",
    )

    # Concurrency control: only one ``agent.arun()`` may run at a time per
    # Agno session.  If the user is currently interacting (SSE stream holds
    # the lock), we DO NOT block — we leave ``notified=False`` so the next
    # user turn (see ``stream_message``/``send_message``) picks up the
    # pending results via ``get_pending_bg_notifications`` and injects them
    # naturally into that turn's context.  This avoids overwriting the
    # in-flight run's history snapshot.
    from src.backend_api.app.services.agno_session_lock import (  # noqa: PLC0415
        publish_conversation_updated,
        try_acquire,
        release,
    )

    lock_token = await try_acquire(
        tenant_session.agno_session_id,
        owner="bg_continuation",
    )
    if lock_token is None:
        log.info(
            "continue_after_bg_task: task=%s session=%s — Agno session busy; "
            "deferring to next user turn (results remain notified=False)",
            task_id, tenant_session.id,
        )
        raise ContinuationSkipped(
            f"Agno session {tenant_session.agno_session_id} busy — deferred"
        )

    # Run agent (MCP cleanup runs in finally to avoid cancel busy-loop).
    try:
        run_output = await agent.arun(prompt)
    finally:
        await _safe_mcp_cleanup(agent)
        await release(tenant_session.agno_session_id, lock_token)

    # Agno swallows provider errors and returns RunOutput with status=RunStatus.error.
    # The error run is already persisted in the Agno session, so the chat history
    # will surface it via list_messages() (see chat.py). We only re-raise on
    # transient errors so the Celery task can retry; for non-transient errors
    # we return a friendly message so the user sees clear feedback.
    from agno.run import RunStatus
    if getattr(run_output, "status", None) == RunStatus.error:
        err_content = str(getattr(run_output, "content", "") or "")
        log.error(
            "continue_after_bg_task: agent run failed — task=%s error=%s",
            task_id, err_content,
        )
        # Mark notified so we don't loop forever on the same bg result
        try:
            await mark_bg_tasks_notified([t.id for t in pending_bg_tasks], db)
            await db.commit()
        except Exception as exc:
            log.warning(
                "continue_after_bg_task: commit of notified flag (after error) failed: %s",
                exc,
            )

        if _is_transient_continuation_error(err_content):
            # Surface as exception so Celery retries.
            raise RuntimeError(f"Agent run failed (transient): {err_content}")

        # Non-transient: return synthesized response so caller delivers it.
        friendly = (
            "I could not finish processing the background task results due to "
            "a problem with the LLM provider. Please try again."
        )
        if err_content:
            friendly = f"{friendly}\n\n_Details: {err_content}_"
        return tenant_session, friendly

    # Mark notified — commit immediately (no enclosing session.begin() here)
    await mark_bg_tasks_notified([t.id for t in pending_bg_tasks], db)
    try:
        await db.commit()
    except Exception as exc:
        log.warning("continue_after_bg_task: commit of notified flag failed: %s", exc)

    # Extract response
    response_text = _extract_text(getattr(run_output, "content", None))
    if not response_text:
        response_text = "Background tasks completed."

    # Check for HITL pause (the agent might request an approval during continuation)
    from agno.run import RunStatus
    if getattr(run_output, "status", None) == RunStatus.paused:
        approval_ids = extract_approval_ids_from_run_output(run_output)
        if approval_ids:
            await process_approval_delegations(
                approval_ids=approval_ids,
                ctx=ctx,
                db=db,
                org=org,
                agno_session_id=tenant_session.agno_session_id,
                run_id=str(getattr(run_output, "run_id", "") or ""),
            )
            try:
                await db.commit()
            except Exception as exc:
                log.warning("continue_after_bg_task: commit of delegations failed: %s", exc)
        # Still deliver partial content if any
        if not response_text or response_text == "Background tasks completed.":
            response_text = (
                "Background tasks completed, but an additional action requires "
                "human approval before proceeding."
            )

    log.info(
        "continue_after_bg_task: task=%s session=%s response_len=%d",
        task_id, tenant_session.id, len(response_text),
    )
    return tenant_session, response_text


# ---------------------------------------------------------------------------
# Public: continue after approval resolution
# ---------------------------------------------------------------------------

async def continue_after_approval(
    approval_id: str,
    org_id: uuid.UUID,
    db: AsyncSession,
) -> tuple[GSageTenantSession, str]:
    """Resume a paused agent run after approval resolution.

    Returns:
        (session, response_text) — caller is responsible for delivery.

    Raises:
        ValueError: if the approval, delegation, or session is not found.
    """
    # Fetch the Agno approval row
    row = await get_agno_db().get_approval(approval_id)
    if row is None:
        raise ContinuationSkipped(
            f"Approval {approval_id} not found in Agno DB — already processed or expired"
        )

    if row.get("status") != "approved":
        log.info(
            "continue_after_approval: approval %s status=%s — skipping continuation",
            approval_id, row.get("status"),
        )
        raise ContinuationSkipped(
            f"Approval {approval_id} is not approved (status={row.get('status')})"
        )

    run_id = row.get("run_id")
    if not run_id:
        raise ValueError(f"Approval {approval_id} has no run_id")

    # Find delegation row for context
    delegation_result = await db.execute(
        select(GSageApprovalDelegation).where(
            GSageApprovalDelegation.approval_id == approval_id
        )
    )
    delegation = delegation_result.scalar_one_or_none()

    # Determine agno_session_id (prefer delegation, fallback to approval row)
    agno_session_id = (
        delegation.agno_session_id if delegation else row.get("session_id")
    )
    if not agno_session_id:
        raise ValueError(f"Cannot determine agno_session_id for approval {approval_id}")

    # Look up the TenantSession
    session_result = await db.execute(
        select(GSageTenantSession).where(
            GSageTenantSession.agno_session_id == agno_session_id,
            GSageTenantSession.org_id == org_id,
        )
    )
    tenant_session = session_result.scalar_one_or_none()
    if tenant_session is None:
        # Fallback: the agno_session_id follows the pattern
        # "org_<org_uuid>:<scope>:<session_uuid>", where the last segment is
        # the GSageTenantSession.id. This handles sessions created by the
        # email worker before agno_session_id was back-filled (NULL column).
        _last_segment = agno_session_id.rsplit(":", 1)[-1]
        try:
            _session_uuid = uuid.UUID(_last_segment)
            fallback_result = await db.execute(
                select(GSageTenantSession).where(
                    GSageTenantSession.id == _session_uuid,
                    GSageTenantSession.org_id == org_id,
                )
            )
            tenant_session = fallback_result.scalar_one_or_none()
            if tenant_session is not None:
                # Back-fill so future lookups (and channel_sender) work correctly.
                tenant_session.agno_session_id = agno_session_id
                log.info(
                    "continue_after_approval: back-filled agno_session_id=%s for session %s",
                    agno_session_id,
                    _session_uuid,
                )
        except (ValueError, Exception) as _exc:
            log.warning(
                "continue_after_approval: fallback UUID lookup failed for %s: %s",
                agno_session_id, _exc,
            )
    if tenant_session is None:
        raise ValueError(f"TenantSession not found for agno_session_id={agno_session_id}")

    # Determine the original requester (from delegation or approval row)
    requester_user_id = (
        delegation.requester_user_id
        if delegation
        else uuid.UUID(row["user_id"])
    )

    interface = _source_to_interface(tenant_session.source)

    # Rebuild context under the ORIGINAL requester's identity.
    # Use the dept_id stored on the delegation (populated at approval-request time)
    # so that department-scoped file access works correctly during continuation,
    # even when session.dept_id is NULL (sessions created before dept_id was persisted).
    override_dept_id = delegation.dept_id if delegation is not None else None
    ctx = await _rebuild_tenant_context(
        tenant_session, db,
        override_user_id=requester_user_id,
        override_dept_id=override_dept_id,
        interface=interface,
    )

    # Load org + user
    org = await db.get(GSageOrganization, org_id)
    user = await db.get(GSageUser, requester_user_id)

    # Build agent (source=continuation — HITL approval flow)
    agent = await _build_agent_for_session(
        tenant_session, ctx, db, org=org, user=user, source="continuation"
    )

    # Mark as continued BEFORE calling acontinue_run() so that any concurrent
    # /continue-run HTTP call will see the flag and return 409.
    if delegation is not None and delegation.continued_at is None:
        from datetime import datetime, timezone as _tz
        delegation.continued_at = datetime.now(_tz.utc)
        try:
            await db.flush()
        except Exception as exc:
            log.warning("continue_after_approval: flush of continued_at failed: %s", exc)

    # Continue the paused run (MCP cleanup in finally to avoid cancel busy-loop).
    run_output = None
    raised_exc: Optional[Exception] = None
    try:
        try:
            run_output = await agent.acontinue_run(run_id=run_id)
        except Exception as exc:
            log.error(
                "continue_after_approval: acontinue_run failed approval=%s run_id=%s: %s",
                approval_id, run_id, exc, exc_info=True,
            )
            raised_exc = exc
    finally:
        await _safe_mcp_cleanup(agent)

    # Handle exception path: re-raise on transient, surface friendly message
    # on non-transient so the user gets feedback in chat.
    if raised_exc is not None:
        err_text = str(raised_exc)
        if _is_transient_continuation_error(err_text):
            raise raised_exc
        friendly = (
            "I could not complete the approved action due to a problem with "
            "the LLM provider. Please try again."
        )
        if err_text:
            friendly = f"{friendly}\n\n_Details: {err_text}_"
        return tenant_session, friendly

    # Handle Agno's swallowed-error path (RunStatus.error returned).
    from agno.run import RunStatus
    if getattr(run_output, "status", None) == RunStatus.error:
        err_content = str(getattr(run_output, "content", "") or "")
        log.error(
            "continue_after_approval: agent run failed approval=%s error=%s",
            approval_id, err_content,
        )
        if _is_transient_continuation_error(err_content):
            raise RuntimeError(f"Agent run failed (transient): {err_content}")
        friendly = (
            "I could not complete the approved action due to a problem with "
            "the LLM provider. Please try again."
        )
        if err_content:
            friendly = f"{friendly}\n\n_Details: {err_content}_"
        return tenant_session, friendly

    response_text = _extract_text(getattr(run_output, "content", None))

    # ── Diagnostic: log run_output status and requirements for nested HITL ──
    _run_status = getattr(run_output, "status", None)
    _run_reqs = getattr(run_output, "requirements", None) or []
    log.debug(
        "continue_after_approval: run_output status=%s requirements_count=%d "
        "approval=%s",
        _run_status, len(_run_reqs), approval_id,
    )

    # ── Re-run agent if it completed without responding to tool results ──
    # When the LLM returns ``finish_reason: stop`` right after a tool
    # result (no assistant text), the user sees nothing.  We probe the
    # Agno session: if the last message is a ``tool`` role, the agent
    # never replied — so we issue ONE follow-up arun() prompting it to
    # analyse the results and present a response.
    if (
        getattr(run_output, "status", None) == RunStatus.completed
        and response_text is not None
    ):
        try:
            from agno.db.base import SessionType  # noqa: PLC0415

            agno_session = await get_agno_db().get_session(
                session_id=agno_session_id,
                session_type=SessionType.AGENT,
            )
            if agno_session is not None:
                runs = list(getattr(agno_session, "runs", None) or [])
                if runs:
                    last_run = runs[-1]
                    msgs = list(getattr(last_run, "messages", None) or [])
                    if msgs:
                        last_msg = msgs[-1]
                        if getattr(last_msg, "role", None) == "tool":
                            log.info(
                                "continue_after_approval: run completed but "
                                "last message is tool — re-prompting agent "
                                "approval=%s",
                                approval_id,
                            )
                            follow_up = await agent.arun(
                                "[SYSTEM_REPROMPT]\n"
                                "The tool execution has completed. "
                                "Please analyse the results and present "
                                "your findings to the user in a clear, "
                                "concise message.\n"
                                "[/SYSTEM_REPROMPT]\n\n---\n"
                            )
                            await _safe_mcp_cleanup(agent)
                            new_text = _extract_text(
                                getattr(follow_up, "content", None)
                            )
                            if new_text and len(new_text) > len(response_text or ""):
                                response_text = new_text

                            # Re-prompt may trigger a new tool call that
                            # requires approval.  Process it the same way
                            # as the main paused check below.
                            if getattr(follow_up, "status", None) == RunStatus.paused:
                                _nested_ids = extract_approval_ids_from_run_output(follow_up)
                                if not _nested_ids:
                                    try:
                                        pending_rows, _ = await get_agno_db().get_approvals(
                                            status="pending", limit=50,
                                        )
                                        for row in pending_rows:
                                            if row.get("session_id") == agno_session_id:
                                                ap_id = row.get("id")
                                                if ap_id:
                                                    _nested_ids.append(str(ap_id))
                                    except Exception:
                                        pass
                                if _nested_ids:
                                    await process_approval_delegations(
                                        approval_ids=_nested_ids,
                                        ctx=ctx, db=db, org=org,
                                        agno_session_id=agno_session_id,
                                        run_id=str(getattr(follow_up, "run_id", "") or ""),
                                    )
                                    auto_ids, _ = await process_auto_approvals(
                                        approval_ids=_nested_ids,
                                        ctx=ctx, db=db,
                                    )
                                    if auto_ids:
                                        log.info(
                                            "continue_after_approval: re-prompt "
                                            "nested auto-approved ids=%s",
                                            auto_ids,
                                        )
                                    try:
                                        await db.commit()
                                    except Exception as exc:
                                        log.warning(
                                            "continue_after_approval: commit of "
                                            "re-prompt delegations failed: %s", exc,
                                        )
                                    if not response_text:
                                        response_text = (
                                            "The approved action has been executed, "
                                            "but an additional step requires human "
                                            "approval before proceeding."
                                        )
        except Exception as exc:
            log.warning(
                "continue_after_approval: re-prompt after tool result "
                "failed approval=%s: %s",
                approval_id, exc,
            )

    # Check if still paused (multi-step HITL)
    if getattr(run_output, "status", None) == RunStatus.paused:
        new_approval_ids = extract_approval_ids_from_run_output(run_output)
        log.debug(
            "continue_after_approval: paused check — extracted %d ids from "
            "run_output.requirements approval=%s",
            len(new_approval_ids), approval_id,
        )
        # Fallback: ``acontinue_run`` may not populate ``requirements`` on
        # the RunOutput when the agent pauses during a continuation.  Query
        # the Agno DB for ALL pending approvals in this session — the nested
        # approval may be on a DIFFERENT run than the one passed to
        # acontinue_run (Agno creates a new run for the continuation).
        if not new_approval_ids:
            try:
                pending_rows, _ = await get_agno_db().get_approvals(
                    status="pending",
                    limit=50,
                )
                for row in pending_rows:
                    if row.get("session_id") == agno_session_id:
                        ap_id = row.get("id")
                        if ap_id:
                            new_approval_ids.append(str(ap_id))
                if new_approval_ids:
                    log.info(
                        "continue_after_approval: resolved %d pending "
                        "approvals via Agno DB fallback session=%s",
                        len(new_approval_ids),
                        agno_session_id,
                    )
            except Exception as exc:
                log.warning(
                    "continue_after_approval: Agno DB fallback query "
                    "failed: %s", exc,
                )
        if new_approval_ids:
            # Create delegation records for the nested approval(s).
            await process_approval_delegations(
                approval_ids=new_approval_ids,
                ctx=ctx,
                db=db,
                org=org,
                agno_session_id=agno_session_id,
                run_id=str(getattr(run_output, "run_id", "") or ""),
            )
            # Auto-approve any that qualify and dispatch their Celery
            # continuation tasks.  Without this step nested approvals
            # would be orphaned — no one processes them.
            auto_ids, manual_ids = await process_auto_approvals(
                approval_ids=new_approval_ids,
                ctx=ctx,
                db=db,
            )
            if auto_ids:
                log.info(
                    "continue_after_approval: nested auto-approved ids=%s",
                    auto_ids,
                )
            try:
                await db.commit()
            except Exception as exc:
                log.warning("continue_after_approval: commit of delegations failed: %s", exc)
        if not response_text:
            response_text = (
                "The approved action has been executed, but an additional step "
                "requires human approval before proceeding."
            )
    else:
        log.debug(
            "continue_after_approval: run_output status is %s (not paused) — "
            "skipping nested HITL check approval=%s",
            getattr(run_output, "status", None), approval_id,
        )

    if not response_text:
        response_text = "The approved action has been executed successfully."

    log.info(
        "continue_after_approval: approval=%s session=%s response_len=%d",
        approval_id, tenant_session.id, len(response_text),
    )
    return tenant_session, response_text


# ---------------------------------------------------------------------------
# Auto-approval processing (shared by SSE stream and nested continuations)
# ---------------------------------------------------------------------------


async def process_auto_approvals(
    *,
    approval_ids: list[str],
    ctx: TenantContext,
    db: AsyncSession,
) -> tuple[list[str], list[str]]:
    """Partition pending approvals into auto-approved vs manual.

    For each id flagged as auto-approve (DB toolconfig > env > default False),
    immediately resolves the Agno approval row as ``approved`` and dispatches
    the continuation Celery task — the same path used when a human clicks
    "Approve" in the UI.

    Returns ``(auto_ids, manual_ids)``. Auto-resolved ids should be excluded
    from delegation processing and from the ``run_paused`` payload emitted
    to the client.

    Errors per approval are logged and the id is treated as manual to fail
    safe (a human is still asked).
    """
    if not approval_ids:
        return [], []

    from datetime import datetime, timezone as _tz
    from typing import Any, cast

    from src.backend_api.app.services.tool_auto_approve import is_auto_approve

    agno_db = get_agno_db()
    auto_ids: list[str] = []
    manual_ids: list[str] = []

    for ap_id in approval_ids:
        try:
            ap_row = await agno_db.get_approval(ap_id)
            if ap_row is None:
                log.warning("auto_approve: approval %s not found in Agno DB", ap_id)
                manual_ids.append(ap_id)
                continue

            tool_name: str = ap_row.get("tool_name") or "*"
            tool_args: dict = dict(ap_row.get("tool_args") or {})
            # Unwrap proxy tool names (run_discovered_tool / run_approved_tool)
            if tool_name in ("run_discovered_tool", "run_approved_tool") and "tool_name" in tool_args:
                tool_name = tool_args["tool_name"] or tool_name

            enabled = await is_auto_approve(
                org_id=ctx.org_id, tool_name=tool_name,
            )
            if not enabled:
                manual_ids.append(ap_id)
                continue

            updated = await agno_db.update_approval(
                ap_id,
                expected_status="pending",
                status="approved",
                resolved_by=str(ctx.user_id),
                resolved_at=int(datetime.now(_tz.utc).timestamp()),
                resolution_data={
                    "action": "approve",
                    "auto_approved": True,
                    "comment": "Auto-approved by tool config",
                },
            )
            if updated is None:
                # The atomic status-guard failed — the row may not exist
                # yet (Agno race) or its status is not "pending".  Check
                # the current state before falling back.
                current = await agno_db.get_approval(ap_id)
                if current is None:
                    log.warning(
                        "auto_approve: approval %s not found after update "
                        "returned None — Agno may not have committed yet",
                        ap_id,
                    )
                    manual_ids.append(ap_id)
                    continue
                cur_status = current.get("status", "")
                if cur_status == "approved":
                    # Already approved (concurrent continuation won the race).
                    # We still need to dispatch the continuation task — the
                    # approval was marked "approved" but the tool hasn't
                    # executed yet (that happens inside acontinue_run).
                    log.info(
                        "auto_approve: approval %s already approved by "
                        "another process — dispatching continuation",
                        ap_id,
                    )
                    try:
                        from src.backend_api.app.tasks.agent_continuation import (  # noqa: PLC0415
                            continue_after_approval_resolved,
                        )
                        cast(Any, continue_after_approval_resolved).delay(
                            ap_id, str(ctx.org_id)
                        )
                    except Exception as cont_exc:
                        log.error(
                            "auto_approve: failed to dispatch continuation "
                            "for already-approved ap=%s: %s",
                            ap_id, cont_exc, exc_info=True,
                        )
                    auto_ids.append(ap_id)
                    continue
                log.warning(
                    "auto_approve: update_approval returned None for ap=%s "
                    "tool=%s current_status=%r — retrying without status guard",
                    ap_id, tool_name, cur_status,
                )
                # Retry without the expected_status check so the update
                # succeeds even if Agno has already transitioned the row
                # (e.g. "pending" → "pending" is a no-op but the column
                # might have been set to a non-standard value by a race).
                updated = await agno_db.update_approval(
                    ap_id,
                    status="approved",
                    resolved_by=str(ctx.user_id),
                    resolved_at=int(datetime.now(_tz.utc).timestamp()),
                    resolution_data={
                        "action": "approve",
                        "auto_approved": True,
                        "comment": "Auto-approved by tool config (retry without status guard)",
                    },
                )
                if updated is None:
                    log.warning(
                        "auto_approve: retry also returned None for ap=%s — "
                        "giving up",
                        ap_id,
                    )
                    manual_ids.append(ap_id)
                    continue

            try:
                from src.backend_api.app.tasks.agent_continuation import (  # noqa: PLC0415
                    continue_after_approval_resolved,
                )
                cast(Any, continue_after_approval_resolved).delay(
                    ap_id, str(ctx.org_id)
                )
            except Exception as cont_exc:
                log.error(
                    "auto_approve: failed to dispatch continuation ap=%s: %s",
                    ap_id, cont_exc, exc_info=True,
                )
            auto_ids.append(ap_id)
            log.info(
                "auto-approval: tool=%s approval=%s org=%s user=%s",
                tool_name, ap_id, ctx.org_id, ctx.user_id,
            )
        except Exception as exc:
            log.error(
                "auto_approve: unexpected error for ap=%s: %s",
                ap_id, exc, exc_info=True,
            )
            manual_ids.append(ap_id)

    return auto_ids, manual_ids


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _source_to_interface(source: str) -> str:
    """Map TenantSession.source to the interface name used by build_agent."""
    return {
        "web": "web",
        "telegram": "telegram",
        "email": "email",
        "scheduled": "web",  # scheduled jobs have no specific interface
        "cli": "web",
        "api": "web",
    }.get(source, "web")
