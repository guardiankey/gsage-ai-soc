"""Admin Console — service functions for Sessions and Agent Runs."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def list_sessions(
    db: AsyncSession,
    org_id: uuid.UUID,
    limit: int = 100,
) -> list[dict[str, Any]]:
    from src.shared.models.user import GSageUser  # noqa: PLC0415
    from src.shared.models.tenant_session import GSageTenantSession  # noqa: PLC0415

    result = await db.execute(
        select(GSageTenantSession, GSageUser.email)
        .outerjoin(GSageUser, GSageUser.id == GSageTenantSession.user_id)
        .where(GSageTenantSession.org_id == org_id)
        .order_by(GSageTenantSession.created_at.desc())
        .limit(limit)
    )
    rows = result.all()
    return [_session_to_dict(s, email) for s, email in rows]


async def list_agent_runs(
    db: AsyncSession,
    session_id: uuid.UUID,
    limit: int = 50,
) -> list[dict[str, Any]]:
    from src.shared.models.agent_run import GSageAgentRun  # noqa: PLC0415

    result = await db.execute(
        select(GSageAgentRun)
        .where(GSageAgentRun.session_id == session_id)
        .order_by(GSageAgentRun.created_at.asc())
        .limit(limit)
    )
    return [_run_to_dict(r) for r in result.scalars().all()]


async def list_recent_runs(
    db: AsyncSession,
    org_id: uuid.UUID,
    limit: int = 20,
) -> list[dict[str, Any]]:
    from src.shared.models.agent_run import GSageAgentRun  # noqa: PLC0415

    result = await db.execute(
        select(GSageAgentRun)
        .where(GSageAgentRun.org_id == org_id)
        .order_by(GSageAgentRun.created_at.desc())
        .limit(limit)
    )
    return [_run_to_dict(r) for r in result.scalars().all()]


async def list_recent_runs_es(
    org_id: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return recent agent runs from Elasticsearch (richer data than PostgreSQL)."""
    import asyncio  # noqa: PLC0415
    from admin_console.db.es_client import es_recent_agent_runs  # noqa: PLC0415

    return await asyncio.to_thread(es_recent_agent_runs, org_id, limit)


def _session_to_dict(s: Any, email: str | None = None) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "org_id": str(s.org_id),
        "user_id": str(s.user_id) if s.user_id else None,
        "user_email": email or "",
        "title": s.title or "(no title)",
        "source": s.source,
        "is_active": s.is_active,
        "created_at": s.created_at.isoformat() if s.created_at else "",
    }


def _run_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "session_id": str(r.session_id),
        "org_id": str(r.org_id),
        "agent_type": r.agent_type,
        "status": r.status,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "duration_ms": r.duration_ms,
        "error_message": r.error_message or "",
        "created_at": r.created_at.isoformat() if r.created_at else "",
    }
