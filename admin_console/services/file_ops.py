"""Admin Console — service functions for Files (DB + MinIO)."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def list_files_db(
    db: AsyncSession,
    org_id: uuid.UUID,
    limit: int = 100,
) -> list[dict[str, Any]]:
    from src.shared.models.generated_file import GSageFile  # noqa: PLC0415

    result = await db.execute(
        select(GSageFile)
        .where(GSageFile.org_id == org_id)
        .order_by(GSageFile.created_at.desc())
        .limit(limit)
    )
    return [_file_to_dict(f) for f in result.scalars().all()]


async def purge_file_record(db: AsyncSession, file_id: uuid.UUID) -> None:
    """Delete the DB record for a file (hard delete for admin)."""
    from sqlalchemy import delete  # noqa: PLC0415

    from src.shared.models.generated_file import GSageFile  # noqa: PLC0415

    await db.execute(delete(GSageFile).where(GSageFile.id == file_id))
    await db.commit()


def list_files_minio(
    bucket: str,
    prefix: Optional[str] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    from admin_console.db.minio_ops import minio_list_objects  # noqa: PLC0415

    return minio_list_objects(bucket, prefix or "", limit)


def get_presigned_url(bucket: str, object_key: str, expires_seconds: int = 3600) -> str:
    from admin_console.db.minio_ops import minio_presigned_url  # noqa: PLC0415

    return minio_presigned_url(bucket, object_key, expires_seconds)


def _file_to_dict(f: Any) -> dict[str, Any]:
    return {
        "id": str(f.id),
        "org_id": str(f.org_id),
        "user_id": str(f.user_id) if f.user_id else "",
        "tool_name": f.tool_name,
        "filename": f.filename,
        "content_type": f.content_type,
        "size_bytes": f.size_bytes,
        "storage_key": f.storage_key,
        "description": f.description or "",
        "category": f.category or "generated",
        "expires_at": f.expires_at.isoformat() if f.expires_at else "",
        "purged_at": f.purged_at.isoformat() if f.purged_at else "",
        "created_at": f.created_at.isoformat() if f.created_at else "",
    }
