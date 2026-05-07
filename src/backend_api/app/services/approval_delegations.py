"""Approval delegation processing — shared across channels.

Extracted from ``chat.py`` so that Telegram handler, scheduled jobs, and the
Agent Continuation Service can all create delegation records and send email
notifications when a run is paused for HITL approval.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.core.tenant import TenantContext
from src.shared.models.approval_delegation import GSageApprovalDelegation
from src.shared.models.organization import GSageOrganization
from src.shared.models.user import GSageUser
from src.shared.services.approval_rule_service import find_approver
from src.shared.services.email_service import send_approval_notification

log = logging.getLogger(__name__)


async def process_approval_delegations(
    *,
    approval_ids: list[str],
    ctx: TenantContext,
    db: AsyncSession,
    org: Optional[GSageOrganization],
    agno_session_id: str,
    run_id: str,
) -> None:
    """For each pending approval, create a delegation record and send email.

    Looks up the requester's display name, resolves the best-matching
    :class:`~src.shared.models.approval_rule.GSageApprovalRule` for the
    tool, writes a :class:`~src.shared.models.approval_delegation.GSageApprovalDelegation`
    row, and sends a notification email to the delegated approver.

    Errors are logged but do not propagate — the caller is not affected.
    """
    # Load requester display name once (reused for all approvals in this run)
    requester_name: str = str(ctx.user_id)
    try:
        req_result = await db.execute(
            select(GSageUser).where(GSageUser.id == ctx.user_id)
        )
        req_user = req_result.scalar_one_or_none()
        if req_user:
            full_name = getattr(req_user, "full_name", None) or getattr(req_user, "name", None)
            requester_name = full_name or str(getattr(req_user, "email", str(ctx.user_id)))
    except Exception as exc:
        log.warning("Could not load requester name for delegation: %s", exc)

    from src.backend_api.app.services.agent_factory import get_agno_db

    for ap_id in approval_ids:
        try:
            # Fetch the Agno approval row for this ID
            ap_row = await get_agno_db().get_approval(ap_id)
            if ap_row is None:
                log.warning("Delegation: approval %s not found in Agno DB", ap_id)
                continue

            tool_name: str = ap_row.get("tool_name") or "*"
            tool_args: dict = dict(ap_row.get("tool_args") or {})

            # Unwrap proxy tool names (run_discovered_tool / run_approved_tool)
            if tool_name in ("run_discovered_tool", "run_approved_tool") and "tool_name" in tool_args:
                real_params = tool_args.get("params")
                tool_name = tool_args["tool_name"] or tool_name
                if isinstance(real_params, dict):
                    tool_args = dict(real_params)

            # Extract agent-generated summary (pop to keep tool_args clean)
            summary: Optional[str] = tool_args.pop("_approval_summary", None)
            if not summary:
                summary = f"{requester_name} solicitou a execução de '{tool_name}'"

            # Resolve approver via rule matching (passing active dept for scope-aware rules)
            approver_id = await find_approver(ctx.org_id, ctx.user_id, tool_name, db, dept_id=ctx.dept_id)
            if approver_id is None:
                log.debug(
                    "Delegation: no rule found for org=%s user=%s tool=%s — no delegation",
                    ctx.org_id, ctx.user_id, tool_name,
                )
                continue

            # Skip if a delegation for this approval_id already exists (idempotent)
            existing = await db.execute(
                select(GSageApprovalDelegation).where(
                    GSageApprovalDelegation.approval_id == ap_id
                )
            )
            if existing.scalar_one_or_none() is not None:
                log.debug("Delegation: approval %s already has a delegation row", ap_id)
                continue

            delegation = GSageApprovalDelegation(
                approval_id=ap_id,
                org_id=ctx.org_id,
                dept_id=getattr(ctx, "dept_id", None),
                requester_user_id=ctx.user_id,
                approver_user_id=approver_id,
                tool_name=tool_name,
                agno_session_id=agno_session_id,
                run_id=run_id,
                summary=summary,
            )
            db.add(delegation)

            # Load approver email for notification
            approver_result = await db.execute(
                select(GSageUser).where(GSageUser.id == approver_id)
            )
            approver_user = approver_result.scalar_one_or_none()
            if approver_user:
                approver_email = str(getattr(approver_user, "email", ""))
                if approver_email:
                    try:
                        await send_approval_notification(
                            to_email=approver_email,
                            tool_name=tool_name,
                            requester_name=requester_name,
                            approval_id=ap_id,
                            summary=summary,
                            org=org,
                        )
                        delegation.notified_at = datetime.now(timezone.utc)
                        log.info(
                            "Approval notification sent: approval=%s approver=%s",
                            ap_id, approver_email,
                        )
                    except Exception as mail_exc:
                        log.warning(
                            "Failed to send approval notification approval=%s: %s",
                            ap_id, mail_exc,
                        )

        except Exception as exc:
            log.error(
                "Error processing delegation for approval=%s: %s",
                ap_id, exc, exc_info=True,
            )

    # NOTE: caller is responsible for committing (or rolling back).
    # Do NOT call db.commit() here — this function may be called inside an
    # existing transaction managed by `async with session.begin():`, and an
    # internal commit would close that managed transaction.


def extract_approval_ids_from_run_output(run_output) -> list[str]:
    """Extract pending approval IDs from a paused RunOutput."""
    approval_ids: list[str] = []
    for req in getattr(run_output, "requirements", None) or []:
        te = getattr(req, "tool_execution", None)
        approval_id = getattr(te, "approval_id", None) if te else None
        if approval_id:
            approval_ids.append(str(approval_id))
    return approval_ids
