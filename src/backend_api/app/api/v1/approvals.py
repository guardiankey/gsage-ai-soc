"""gSage AI — Approvals (HITL) routes.

Routes
------
GET  /orgs/{org_id}/approvals                   List pending approvals for the tenant
GET  /orgs/{org_id}/approvals/{approval_id}     Get a single approval
POST /orgs/{org_id}/approvals/{approval_id}/resolve   Approve or reject

Tenant isolation
----------------
Agno stores approvals with a ``user_id`` field (the user who initiated the
run) and a ``session_id`` (of the form ``org_<org_id>:<scope>:<id>``).

* Regular members see only their own approvals (filtered by ``user_id``).
* Admins and owners can additionally pass ``?target_user_id=<uuid>`` to
  inspect another org member's approvals, which is useful for oversight.

Resolving an approval is allowed to whoever can *read* it (either the
owner or an org admin).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_db, get_tenant_context
from src.backend_api.app.core.tenant import TenantContext
from src.backend_api.app.schemas.approvals import (
    ApprovalListResponse,
    ApprovalOut,
    ApprovalResolve,
    PendingCountResponse,
)
from src.backend_api.app.schemas.chat import (
    MessageMetadata,
    MessageTokenMetadata,
    SendMessageResponse,
)
from src.backend_api.app.services.agent_factory import (
    DEFAULT_AGENT_ID,
    build_agent,
    load_interface_profiles,
    get_agno_db,
)
from src.shared.models.approval_delegation import GSageApprovalDelegation
from src.shared.models.organization import GSageOrganization
from src.shared.models.tenant_session import GSageTenantSession
from src.shared.models.user import GSageUser
from src.shared.models.user_organization import GSageUserOrganization

log = logging.getLogger(__name__)

router = APIRouter()

# Status strings used by Agno
_STATUS_PENDING = "pending"
_STATUS_APPROVED = "approved"
_STATUS_REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Proxy function names used by the dual-proxy tool discovery pattern.
# When the LLM invokes a non-core tool, Agno records the proxy name as the
# tool_name.  We unwrap these so the UI shows the real underlying tool.
_PROXY_TOOL_NAMES = frozenset({"run_discovered_tool", "run_approved_tool"})


def _unwrap_proxy_tool(tool_name: str | None, tool_args: dict | None) -> tuple[str | None, dict | None]:
    """If *tool_name* is a proxy function, extract the real tool name and args.

    Returns ``(real_tool_name, real_tool_args)``.
    """
    if tool_name in _PROXY_TOOL_NAMES and isinstance(tool_args, dict):
        real_name = tool_args.get("tool_name") or tool_name
        real_args = tool_args.get("params") if isinstance(tool_args.get("params"), dict) else tool_args
        return real_name, real_args
    return tool_name, tool_args


def _approval_row_to_out(
    row: dict,
    delegation: Optional[GSageApprovalDelegation] = None,
    approver_name: Optional[str] = None,
    requester_name: Optional[str] = None,
) -> ApprovalOut:
    tool_name, tool_args = _unwrap_proxy_tool(row.get("tool_name"), row.get("tool_args"))
    return ApprovalOut(
        id=str(row.get("id", "")),
        run_id=row.get("run_id"),
        session_id=row.get("session_id"),
        status=row.get("status"),
        approval_type=row.get("approval_type"),
        source_type=row.get("source_type"),
        pause_type=row.get("pause_type"),
        tool_name=tool_name,
        tool_args=tool_args,
        agent_id=row.get("agent_id"),
        user_id=row.get("user_id"),
        context=row.get("context"),
        requirements=row.get("requirements"),
        resolution_data=row.get("resolution_data"),
        resolved_by=row.get("resolved_by"),
        resolved_at=row.get("resolved_at"),
        expires_at=row.get("expires_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        # Delegation enrichment
        delegated_to_user_id=str(delegation.approver_user_id) if delegation else None,
        delegated_to_user_name=approver_name,
        requester_user_name=requester_name,
        summary=delegation.summary if delegation else None,
    )


async def _load_user_name(user_id: uuid.UUID, db: AsyncSession) -> Optional[str]:
    """Return a display name (full name or email) for *user_id*."""
    result = await db.execute(
        select(GSageUser).where(GSageUser.id == user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        return None
    full_name = getattr(user, "full_name", None) or getattr(user, "name", None)
    return full_name or str(getattr(user, "email", str(user_id)))


async def _assert_org_member(
    target_user_id: uuid.UUID,
    org_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    """Raise 403 if *target_user_id* is not a member of *org_id*."""
    result = await db.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == target_user_id,
            GSageUserOrganization.org_id == org_id,
            GSageUserOrganization.is_active == True,  # noqa: E712
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of this organization",
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/orgs/{org_id}/approvals",
    response_model=ApprovalListResponse,
    summary="List approvals for the tenant",
)
async def list_approvals(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
    approval_status: Optional[str] = Query(None, alias="status"),
    target_user_id: Optional[uuid.UUID] = Query(
        None,
        description="Admin-only: filter approvals for a different org member",
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
) -> ApprovalListResponse:
    ctx.require_permission("approvals:read")

    # Determine which user's approvals to list
    if target_user_id is not None and target_user_id != ctx.user_id:
        # Only admins / owners may view another user's approvals
        if ctx.org_role not in ("admin", "owner"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin role required to view another user's approvals",
            )
        await _assert_org_member(target_user_id, org_id, db)
        effective_user_id = str(target_user_id)
    else:
        effective_user_id = str(ctx.user_id)

    rows, total = await get_agno_db().get_approvals(
        status=approval_status,
        user_id=effective_user_id,
        limit=limit,
        page=page,
    )

    # Fetch delegations for this user so we can enrich own approvals
    own_delegation_result = await db.execute(
        select(GSageApprovalDelegation).where(
            GSageApprovalDelegation.requester_user_id == uuid.UUID(effective_user_id),
        )
    )
    own_delegations: dict[str, GSageApprovalDelegation] = {
        d.approval_id: d for d in own_delegation_result.scalars().all()
    }

    # Also include approvals delegated TO the current user (if not already listed)
    delegated_to_me_result = await db.execute(
        select(GSageApprovalDelegation).where(
            GSageApprovalDelegation.approver_user_id == ctx.user_id,
            GSageApprovalDelegation.org_id == ctx.org_id,
        )
    )
    delegated_to_me: list[GSageApprovalDelegation] = list(
        delegated_to_me_result.scalars().all()
    )

    # Collect extra approval IDs delegated to me that aren't already in rows
    existing_ids = {str(r.get("id", "")) for r in rows}
    extra_rows: list[dict] = []
    for d in delegated_to_me:
        if d.approval_id not in existing_ids:
            if approval_status and d.approval_id:
                extra_row = await get_agno_db().get_approval(d.approval_id)
                if extra_row and (approval_status is None or extra_row.get("status") == approval_status):
                    extra_rows.append(extra_row)
            elif d.approval_id:
                extra_row = await get_agno_db().get_approval(d.approval_id)
                if extra_row:
                    extra_rows.append(extra_row)

    all_rows = list(rows) + extra_rows
    combined_total = total + len(extra_rows)

    # Build a unified delegation map: approval_id → delegation
    all_delegation_map: dict[str, GSageApprovalDelegation] = {**own_delegations}
    for d in delegated_to_me:
        all_delegation_map[d.approval_id] = d

    # Enrich items with delegation metadata
    items: list[ApprovalOut] = []
    for r in all_rows:
        ap_id = str(r.get("id", ""))
        delegation = all_delegation_map.get(ap_id)
        approver_name: Optional[str] = None
        requester_name: Optional[str] = None
        if delegation:
            approver_name = await _load_user_name(delegation.approver_user_id, db)
            requester_name = await _load_user_name(delegation.requester_user_id, db)
        items.append(_approval_row_to_out(r, delegation, approver_name, requester_name))

    return ApprovalListResponse(items=items, total=combined_total, page=page, limit=limit)


@router.get(
    "/orgs/{org_id}/approvals/pending-count",
    response_model=PendingCountResponse,
    summary="Count pending approvals for the current user",
)
async def get_pending_count(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
) -> PendingCountResponse:
    """Lightweight endpoint — returns only the count of pending approvals.

    Includes approvals owned by the current user AND approvals delegated to
    the current user from other users.  Intended for global polling (TopNav
    badge) without transferring full approval objects.
    """
    ctx.require_permission("approvals:read")

    effective_user_id = str(ctx.user_id)

    # Own pending approvals — use limit=1 so we just get the total count
    own_rows, own_total = await get_agno_db().get_approvals(
        status=_STATUS_PENDING,
        user_id=effective_user_id,
        limit=1,
        page=1,
    )
    existing_ids: set[str] = {str(r.get("id", "")) for r in own_rows}

    # Approvals delegated TO me that aren't in own list
    delegated_result = await db.execute(
        select(GSageApprovalDelegation).where(
            GSageApprovalDelegation.approver_user_id == ctx.user_id,
            GSageApprovalDelegation.org_id == ctx.org_id,
        )
    )
    delegated_to_me: list[GSageApprovalDelegation] = list(
        delegated_result.scalars().all()
    )

    extra_count = 0
    for d in delegated_to_me:
        if not d.approval_id or d.approval_id in existing_ids:
            continue
        row = await get_agno_db().get_approval(d.approval_id)
        if row and row.get("status") == _STATUS_PENDING:
            extra_count += 1

    return PendingCountResponse(count=own_total + extra_count)


@router.get(
    "/orgs/{org_id}/approvals/{approval_id}",
    response_model=ApprovalOut,
    summary="Get a single approval",
)
async def get_approval(
    org_id: uuid.UUID,
    approval_id: str,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
) -> ApprovalOut:
    ctx.require_permission("approvals:read")

    row = await get_agno_db().get_approval(approval_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")

    # Tenant isolation: the approval must belong to this user or an admin
    if row.get("user_id") != str(ctx.user_id) and ctx.org_role not in ("member", "admin", "owner"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    # For admins, verify the user who owns the approval is in this org
    if row.get("user_id") != str(ctx.user_id):
        try:
            owner_id = uuid.UUID(row["user_id"])
            await _assert_org_member(owner_id, org_id, db)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found"
            )

    return _approval_row_to_out(row)


@router.post(
    "/orgs/{org_id}/approvals/{approval_id}/resolve",
    response_model=ApprovalOut,
    summary="Approve or reject a pending approval",
)
async def resolve_approval(
    org_id: uuid.UUID,
    approval_id: str,
    payload: ApprovalResolve,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
) -> ApprovalOut:
    ctx.require_permission("approvals:resolve")

    # Fetch and authorise
    row = await get_agno_db().get_approval(approval_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")

    # Check if the current user is the delegated approver for this approval
    delegation_result = await db.execute(
        select(GSageApprovalDelegation).where(
            GSageApprovalDelegation.approval_id == approval_id
        )
    )
    delegation = delegation_result.scalar_one_or_none()
    is_delegated_approver = (
        delegation is not None
        and delegation.approver_user_id == ctx.user_id
    )

    is_owner = row.get("user_id") == str(ctx.user_id)
    is_admin = ctx.org_role in ("admin", "owner")
    if not (is_owner or is_admin or is_delegated_approver):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    if row.get("status") != _STATUS_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Approval is not pending (current status: {row.get('status')})",
        )

    new_status = _STATUS_APPROVED if payload.action == "approve" else _STATUS_REJECTED

    resolution: dict = {"action": payload.action}
    if payload.comment:
        resolution["comment"] = payload.comment

    updated = await get_agno_db().update_approval(
        approval_id,
        expected_status=_STATUS_PENDING,
        status=new_status,
        resolved_by=str(ctx.user_id),
        resolved_at=int(datetime.now(timezone.utc).timestamp()),
        resolution_data=resolution,
    )

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Approval could not be resolved (already resolved or not found)",
        )

    # Auto-continue: dispatch Celery task to resume the agent run when approved.
    # The backend now handles continuation automatically — the frontend only needs
    # to call resolve, not continue-run.
    if new_status == _STATUS_APPROVED:
        try:
            from src.backend_api.app.tasks.agent_continuation import (
                continue_after_approval_resolved,
            )
            continue_after_approval_resolved.delay(approval_id, str(org_id))
            log.info(
                "resolve_approval: dispatched continuation for approval=%s org=%s",
                approval_id, org_id,
            )
        except Exception as cont_exc:
            log.warning(
                "resolve_approval: failed to dispatch continuation for approval=%s: %s",
                approval_id, cont_exc,
            )

    return _approval_row_to_out(updated)


@router.post(
    "/orgs/{org_id}/approvals/{approval_id}/continue-run",
    response_model=SendMessageResponse,
    summary="Continue the agent run after an approval has been resolved",
)
async def continue_run_after_approval(
    org_id: uuid.UUID,
    approval_id: str,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: AsyncSession = Depends(get_db),
) -> SendMessageResponse:
    """Resume the paused agent run associated with this approval.

    Must be called after ``POST .../approvals/{id}/resolve`` with action=approve.
    Looks up the session from the approval, builds the agent, and calls
    ``acontinue_run``.  Returns the same shape as the send-message endpoint.
    """
    ctx.require_permission("agents:run")

    # ── Fetch and authorise ──────────────────────────────────────────────
    row = await get_agno_db().get_approval(approval_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")

    # Check delegation — the delegated approver may also resume the run
    delegation_result = await db.execute(
        select(GSageApprovalDelegation).where(
            GSageApprovalDelegation.approval_id == approval_id
        )
    )
    delegation = delegation_result.scalar_one_or_none()
    is_delegated_approver = (
        delegation is not None
        and delegation.approver_user_id == ctx.user_id
    )

    is_owner = row.get("user_id") == str(ctx.user_id)
    is_admin = ctx.org_role in ("admin", "owner")
    if not (is_owner or is_admin or is_delegated_approver):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    if row.get("status") != _STATUS_APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Approval is not approved (status: {row.get('status')}). "
                "Resolve it first via POST .../resolve"
            ),
        )

    run_id = row.get("run_id")
    # Use session from delegation if available (more reliable than agno row)
    agno_session_id = (
        delegation.agno_session_id if delegation else row.get("session_id")
    )
    if not run_id or not agno_session_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Approval is missing run_id or session_id — cannot resume run",
        )

    # Guard: if acontinue_run was already dispatched (by the auto-continuation
    # Celery task or a previous call to this endpoint), reject to prevent
    # duplicate agent runs and duplicate bg task creation.
    if delegation is not None and delegation.continued_at is not None:
        log.info(
            "continue_run_after_approval: approval=%s already continued at %s — skipping",
            approval_id, delegation.continued_at,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This approval run has already been continued. "
                "The response will be delivered automatically via the originating channel."
            ),
        )

    # Mark as continued BEFORE calling acontinue_run() to prevent races.
    if delegation is not None:
        delegation.continued_at = datetime.now(timezone.utc)
        await db.flush()

    # ── Look up the conversation session ────────────────────────────────
    result = await db.execute(
        select(GSageTenantSession).where(
            GSageTenantSession.agno_session_id == agno_session_id,
            GSageTenantSession.org_id == ctx.org_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation session not found for this approval",
        )

    # ── Load org and build agent ─────────────────────────────────────────
    org_result = await db.execute(
        select(GSageOrganization).where(GSageOrganization.id == ctx.org_id)
    )
    org = org_result.scalar_one_or_none()

    # When the run was delegated, the agent must be built using the ORIGINAL
    # requester's identity (not the delegated approver's) so tool permissions
    # and session ownership are correct.
    agent_ctx = ctx
    if is_delegated_approver and delegation is not None:
        from src.backend_api.app.core.tenant import TenantContext as TC

        # Load the requester's org membership to get their role
        membership_result = await db.execute(
            select(GSageUserOrganization).where(
                GSageUserOrganization.user_id == delegation.requester_user_id,
                GSageUserOrganization.org_id == ctx.org_id,
                GSageUserOrganization.is_active == True,  # noqa: E712
            )
        )
        membership = membership_result.scalar_one_or_none()
        if membership is not None:
            agent_ctx = TC(
                user_id=delegation.requester_user_id,
                org_id=ctx.org_id,
                org_role=membership.role,
                permissions=ctx.permissions,  # keep current permission set
            )
        else:
            log.warning(
                "continue_run: delegated requester %s has no membership in org %s — "
                "falling back to approver context",
                delegation.requester_user_id,
                ctx.org_id,
            )

    profile_org, profile_user = await load_interface_profiles(
        agent_ctx.org_id, agent_ctx.user_id, agent_ctx.interface, db
    )

    # Load the effective user (requester when delegated, otherwise approver)
    # so the agent system prompt carries the correct identity.
    user_result = await db.execute(
        select(GSageUser).where(GSageUser.id == agent_ctx.user_id)
    )
    agent_user = user_result.scalar_one_or_none()

    agent = build_agent(
        ctx=agent_ctx,
        agent_id=DEFAULT_AGENT_ID,
        session_id=agno_session_id,
        org=org,
        user=agent_user,
        interface_profile_org=profile_org,
        interface_profile_user=profile_user,
        gsage_session_id=session.id,
    )

    try:
        try:
            run_output = await agent.acontinue_run(run_id=run_id)
        except Exception as exc:
            log.error(
                "acontinue_run failed approval=%s run_id=%s: %s",
                approval_id, run_id, exc, exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Agent error resuming run: {exc}",
            )

        from agno.run import RunStatus
        from src.backend_api.app.api.v1.chat import _extract_text

        # Still paused — more approvals required (multi-step HITL)
        if run_output.status == RunStatus.paused:
            pending_approval_ids: list[str] = []
            for req in run_output.requirements or []:
                te = getattr(req, "tool_execution", None)
                aid = getattr(te, "approval_id", None) if te else None
                if aid:
                    pending_approval_ids.append(str(aid))

            return SendMessageResponse(
                id=run_output.run_id or str(uuid.uuid4()),
                session_id=str(session.id),
                agno_session_id=agno_session_id,
                role="assistant",
                content=_extract_text(run_output.content) or (
                    "Additional approvals are required. Resolve all pending approvals "
                    "and call this endpoint (or POST .../messages/continue) again."
                ),
                created_at=datetime.now(timezone.utc),
                metadata=MessageMetadata(run_id=run_output.run_id),
                status="pending_approval",
                pending_run_id=run_output.run_id,
                pending_approvals=pending_approval_ids or None,
            )

        content = _extract_text(run_output.content)
        metrics = getattr(run_output, "metrics", None)

        return SendMessageResponse(
            id=run_output.run_id or str(uuid.uuid4()),
            session_id=str(session.id),
            agno_session_id=agno_session_id,
            role="assistant",
            content=content or "",
            created_at=datetime.now(timezone.utc),
            metadata=MessageMetadata(
                run_id=run_output.run_id,
                tokens=MessageTokenMetadata(
                    input=getattr(metrics, "input_tokens", None) if metrics else None,
                    output=getattr(metrics, "output_tokens", None) if metrics else None,
                ) if metrics else None,
                duration_ms=(
                    int(getattr(metrics, "duration", 0) * 1000)
                    if metrics and getattr(metrics, "duration", None)
                    else None
                ),
            ),
        )
    finally:
        # Cleanup MCP sessions to prevent anyio cancel busy-loop (100% CPU).
        try:
            from src.shared.services.mcp_cleanup import cleanup_agent_mcp

            await cleanup_agent_mcp(agent)
        except Exception:
            log.debug("MCP cleanup failed (ignored)", exc_info=True)
