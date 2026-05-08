"""gSage AI — Programmatic agent routes (Sprint 3).

Routes
------
- ``GET  /orgs/{org_id}/agents``               — list available agents for this org
- ``GET  /orgs/{org_id}/agents/{agent_id}``    — get single agent details
- ``POST /orgs/{org_id}/agents/{agent_id}/run`` — execute an agent run
"""

from __future__ import annotations

import uuid
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_tenant_context
from src.backend_api.app.core.tenant import TenantContext
from src.backend_api.app.api.v1.chat import _cleanup_agent_mcp
import logging

from src.backend_api.app.schemas.chat import AgentInfo, AgentRunRequest, AgentRunResponse
from src.backend_api.app.services.agent_factory import AGENT_REGISTRY, _fetch_tool_catalog, build_agent, load_interface_profiles
from src.shared.database import get_db
from src.shared.models.organization import GSageOrganization
from src.shared.models.user import GSageUser

log = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/orgs/{org_id}/agents",
    response_model=List[AgentInfo],
    summary="List agents available for this org",
)
async def list_agents(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> List[AgentInfo]:
    """Return the global agent registry (V1 — no per-org overrides yet)."""
    ctx.require_permission("agents:read")
    return [AgentInfo(id=k, **v) for k, v in AGENT_REGISTRY.items()]


@router.get(
    "/orgs/{org_id}/agents/{agent_id}",
    response_model=AgentInfo,
    summary="Get agent details",
)
async def get_agent(
    org_id: uuid.UUID,
    agent_id: str,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
) -> AgentInfo:
    """Return metadata for a single agent. Raises 404 if not registered."""
    ctx.require_permission("agents:read")
    meta = AGENT_REGISTRY.get(agent_id)
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{agent_id}' not found.",
        )
    return AgentInfo(id=agent_id, **meta)


@router.post(
    "/orgs/{org_id}/agents/{agent_id}/run",
    response_model=AgentRunResponse,
    summary="Execute an agent run",
)
async def run_agent(
    org_id: uuid.UUID,
    agent_id: str,
    payload: AgentRunRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentRunResponse:
    """Execute an agent with a message and optional session continuation.

    If ``session_id`` is omitted a new tenant-scoped session is created.
    Streaming is not supported on this endpoint — use the Chat Client stream
    endpoint instead (POST .../chat/conversations/{id}/messages/stream).
    """
    ctx.require_permission("agents:run")

    if agent_id not in AGENT_REGISTRY:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{agent_id}' not found.",
        )

    if payload.stream:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Streaming not supported on this endpoint. Use the /messages/stream route.",
        )

    session_id = payload.session_id or ctx.build_session_id("user", str(ctx.user_id))

    result = await db.execute(
        select(GSageOrganization).where(GSageOrganization.id == ctx.org_id)
    )
    org = result.scalar_one_or_none()
    user_result = await db.execute(
        select(GSageUser).where(GSageUser.id == ctx.user_id)
    )
    user = user_result.scalar_one_or_none()
    profile_org, profile_user = await load_interface_profiles(
        ctx.org_id, ctx.user_id, ctx.interface, db
    )
    tool_catalog = await _fetch_tool_catalog(ctx)
    agent = build_agent(
        ctx=ctx,
        agent_id=agent_id,
        session_id=session_id,
        org=org,
        user=user,
        interface_profile_org=profile_org,
        interface_profile_user=profile_user,
        tool_catalog=tool_catalog,
    )

    try:
        from src.shared.services.kb_context import prepend_kb_hints

        effective_message = await prepend_kb_hints(
            payload.message,
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            dept_id=ctx.dept_id,
        )
        run_output = await agent.arun(effective_message)
    except Exception as exc:
        log.error("Agent run failed agent=%s: %s", agent_id, exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Agent error: {exc}",
        )
    finally:
        await _cleanup_agent_mcp(agent)

    from agno.run import RunStatus

    # Agno swallows provider errors and returns RunOutput with status=RunStatus.error.
    if getattr(run_output, "status", None) == RunStatus.error:
        log.error(
            "Agent run returned error agent=%s: %s",
            agent_id, getattr(run_output, "content", ""),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "We tried to reach the LLM service but were unable to get a response. "
                "Please try again later or contact your administrator."
            ),
        )

    content = run_output.content or ""
    if isinstance(content, list):
        content = " ".join(
            str(c.get("text", c)) if isinstance(c, dict) else str(c) for c in content
        )

    metrics = getattr(run_output, "metrics", None)
    return AgentRunResponse(
        run_id=run_output.run_id,
        session_id=run_output.session_id or session_id,
        agent_id=agent_id,
        content=content,
        input_tokens=getattr(metrics, "input_tokens", None) if metrics else None,
        output_tokens=getattr(metrics, "output_tokens", None) if metrics else None,
        duration_ms=(
            int(getattr(metrics, "duration", 0) * 1000)
            if metrics and getattr(metrics, "duration", None)
            else None
        ),
    )
