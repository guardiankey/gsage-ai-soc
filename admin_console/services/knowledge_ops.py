"""Admin Console — service functions for Knowledge Base (Weaviate)."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def list_ingest_jobs(
    db: AsyncSession,
    org_id: uuid.UUID,
    limit: int = 100,
) -> list[dict[str, Any]]:
    from src.shared.models.ingest_job import GSageIngestJob  # noqa: PLC0415

    result = await db.execute(
        select(GSageIngestJob)
        .where(GSageIngestJob.org_id == org_id)
        .order_by(GSageIngestJob.created_at.desc())
        .limit(limit)
    )
    return [_job_to_dict(j) for j in result.scalars().all()]


async def kb_stats(db: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    """Return ingest job counts grouped by status."""
    from src.shared.models.ingest_job import GSageIngestJob  # noqa: PLC0415

    result = await db.execute(
        select(GSageIngestJob.status, func.count())
        .where(GSageIngestJob.org_id == org_id)
        .group_by(GSageIngestJob.status)
    )
    return {row[0]: row[1] for row in result.all()}


async def search_kb(
    org_id: str,
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """BM25 search in Weaviate for org-scoped knowledge."""
    from admin_console.db.weaviate_ops import weaviate_search  # noqa: PLC0415

    return await weaviate_search(
        collection="GSageKnowledge",
        query=query,
        limit=limit,
        org_id=org_id,
    )


async def delete_kb_object(weaviate_uuid: str) -> tuple[bool, str]:
    from admin_console.db.weaviate_ops import weaviate_delete_object  # noqa: PLC0415

    return await weaviate_delete_object("GSageKnowledge", weaviate_uuid)


def _job_to_dict(j: Any) -> dict[str, Any]:
    return {
        "id": str(j.id),
        "org_id": str(j.org_id),
        "scope": j.scope,
        "original_filename": j.original_filename,
        "file_size": j.file_size,
        "status": j.status,
        "chunks_stored": j.chunks_stored or 0,
        "error_message": j.error_message or "",
        "created_at": j.created_at.isoformat() if j.created_at else "",
    }
