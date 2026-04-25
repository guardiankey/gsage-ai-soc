"""gSage AI — Programmatic session routes (Sprint 3).

Routes
------
- ``GET    /orgs/{org_id}/sessions``                  — list tenant sessions
- ``GET    /orgs/{org_id}/sessions/{session_id}``     — session detail
- ``DELETE /orgs/{org_id}/sessions/{session_id}``     — soft-delete (is_active=False)
"""

from __future__ import annotations

import uuid
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_tenant_context
from src.backend_api.app.core.tenant import TenantContext
from src.backend_api.app.schemas.chat import ConversationOut
from src.backend_api.app.schemas.pagination import PaginatedResponse, PaginationParams, paginate_query
from src.shared.database import get_db
from src.shared.models.tenant_session import GSageTenantSession

router = APIRouter()


@router.get(
    "/orgs/{org_id}/sessions",
    response_model=PaginatedResponse[ConversationOut],
    summary="List tenant sessions (programmatic)",
)
async def list_sessions(
    org_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends()],
    active_only: bool = Query(default=False, alias="active"),
) -> PaginatedResponse[ConversationOut]:
    """List ``gsage_tenant_sessions`` for this org.

    Admins (``sessions:read:all``) see all sessions; regular members see only
    their own.
    """
    see_all = ctx.has_permission("sessions:read:all")
    if not see_all:
        ctx.require_permission("sessions:read")

    stmt = select(GSageTenantSession).where(
        GSageTenantSession.org_id == ctx.org_id,
    )
    if not see_all:
        stmt = stmt.where(GSageTenantSession.user_id == ctx.user_id)
    if active_only:
        stmt = stmt.where(GSageTenantSession.is_active == True)  # noqa: E712
    stmt = stmt.order_by(GSageTenantSession.updated_at.desc())

    sessions, total = await paginate_query(db, stmt, pagination)
    items = [
        ConversationOut(
            id=s.id,
            agno_session_id=s.agno_session_id,
            title=s.title,
            is_active=s.is_active,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in sessions
    ]
    return PaginatedResponse.build(items, total=total, pagination=pagination)


@router.get(
    "/orgs/{org_id}/sessions/{session_id}",
    response_model=ConversationOut,
    summary="Get session detail",
)
async def get_session(
    org_id: uuid.UUID,
    session_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationOut:
    """Return a single session. Validates org ownership."""
    ctx.require_permission("sessions:read")

    result = await db.execute(
        select(GSageTenantSession).where(
            GSageTenantSession.id == session_id,
            GSageTenantSession.org_id == ctx.org_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # Non-admins may only see their own sessions
    if not ctx.has_permission("sessions:read:all") and session.user_id != ctx.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    return ConversationOut(
        id=session.id,
        agno_session_id=session.agno_session_id,
        title=session.title,
        is_active=session.is_active,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


@router.delete(
    "/orgs/{org_id}/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a session",
)
async def delete_session(
    org_id: uuid.UUID,
    session_id: uuid.UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Set ``is_active = False``. Agno history is preserved for audit."""
    ctx.require_permission("sessions:delete")

    result = await db.execute(
        select(GSageTenantSession).where(
            GSageTenantSession.id == session_id,
            GSageTenantSession.org_id == ctx.org_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    session.is_active = False
    await db.commit()
