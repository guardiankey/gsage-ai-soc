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
    interface: str = "web",
) -> TenantContext:
    """Rebuild a TenantContext from a stored TenantSession row."""
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

    return TenantContext(
        user_id=user_id,
        org_id=session.org_id,
        org_role=role,
        permissions=permissions_for_role(role),
        interface=interface,
        dept_id=session.dept_id,
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

    if task.status != BackgroundTaskStatus.COMPLETED:
        raise ValueError(f"Background task {task_id} is not COMPLETED (status={task.status})")

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

    # Run agent (MCP cleanup runs in finally to avoid cancel busy-loop).
    try:
        run_output = await agent.arun(prompt)
    finally:
        await _safe_mcp_cleanup(agent)

    # Agno swallows provider errors and returns RunOutput with status=RunStatus.error.
    from agno.run import RunStatus
    if getattr(run_output, "status", None) == RunStatus.error:
        log.error(
            "continue_after_bg_task: agent run failed — task=%s error=%s",
            task_id, getattr(run_output, "content", ""),
        )
        raise RuntimeError(
            f"Agent run failed: {getattr(run_output, 'content', 'LLM provider error')}"
        )

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

    # Rebuild context under the ORIGINAL requester's identity
    ctx = await _rebuild_tenant_context(
        tenant_session, db, override_user_id=requester_user_id, interface=interface
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
    try:
        try:
            run_output = await agent.acontinue_run(run_id=run_id)
        except Exception as exc:
            log.error(
                "continue_after_approval: acontinue_run failed approval=%s run_id=%s: %s",
                approval_id, run_id, exc, exc_info=True,
            )
            raise
    finally:
        await _safe_mcp_cleanup(agent)

    response_text = _extract_text(getattr(run_output, "content", None))

    # Check if still paused (multi-step HITL)
    from agno.run import RunStatus
    if getattr(run_output, "status", None) == RunStatus.paused:
        new_approval_ids = extract_approval_ids_from_run_output(run_output)
        if new_approval_ids:
            await process_approval_delegations(
                approval_ids=new_approval_ids,
                ctx=ctx,
                db=db,
                org=org,
                agno_session_id=agno_session_id,
                run_id=str(getattr(run_output, "run_id", "") or ""),
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

    if not response_text:
        response_text = "The approved action has been executed successfully."

    log.info(
        "continue_after_approval: approval=%s session=%s response_len=%d",
        approval_id, tenant_session.id, len(response_text),
    )
    return tenant_session, response_text


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
