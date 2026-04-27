"""gSage AI — IngestJob model.

Tracks the lifecycle of a document upload processed asynchronously by the
Celery ``knowledge`` queue.

Status transitions::

    queued → processing → completed
                       ↘ failed
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class IngestScope(str):
    """Allowed values for GSageIngestJob.scope."""

    ORG = "org"
    USER = "user"
    DEPT = "dept"


class IngestStatus(str):
    """Allowed values for GSageIngestJob.status."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class GSageIngestJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Document ingest job record.

    Created synchronously by the upload endpoint; updated progressively by
    the Celery ``ingest_document`` task.

    Columns
    -------
    org_id
        Owning organisation.
    user_id
        User who performed the upload.
    scope
        ``"org"`` — stored in the org-level Weaviate collection with no
        user filter.  ``"user"`` — same collection but metadata includes
        ``user_id`` so search can filter by user.
    original_filename
        Sanitised original filename (no path traversal characters).
    file_size
        File size in bytes at upload time.
    status
        Current processing status: queued → processing → completed | failed.
    chunks_stored
        Number of text chunks successfully written to Weaviate.
    error_message
        Non-null if status is ``"failed"``.
    """

    __tablename__ = "gsage_ingest_jobs"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dept_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Department context for this ingest. NULL = org-wide knowledge.",
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="CASCADE"),
        nullable=False,
    )

    scope: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default=IngestScope.ORG,
        comment="'org' or 'user'",
    )

    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)

    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=IngestStatus.QUEUED,
        index=True,
        comment="queued | processing | completed | failed",
    )

    chunks_stored: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    storage_key: Mapped[Optional[str]] = mapped_column(
        String(1000),
        nullable=True,
        comment="MinIO object key in the kb-originals bucket. NULL for pre-existing jobs.",
    )

    source_url: Mapped[Optional[str]] = mapped_column(
        String(2000),
        nullable=True,
        comment="Origin URL when the job was created from a URL submission. NULL for direct file uploads.",
    )

    __table_args__ = (
        Index("ix_ingest_job_org_status", "org_id", "status"),
        Index("ix_ingest_job_user", "user_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<GSageIngestJob(id={self.id}, "
            f"org={self.org_id}, "
            f"file={self.original_filename!r}, "
            f"status={self.status})>"
        )
