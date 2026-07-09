"""gSage AI — GSageInteraction SQLAlchemy model."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as SA_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base


class GSageInteraction(Base):
    """Persistent record of a user interaction (form, confirm, upload, …)."""

    __tablename__ = "gsage_interactions"

    id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        SA_UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id"),
        nullable=False,
    )

    # ── Traceability ──────────────────────────────────────────────────────
    gsage_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        SA_UUID(as_uuid=True), nullable=True
    )
    execution_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        SA_UUID(as_uuid=True), nullable=True
    )
    tool_call_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        SA_UUID(as_uuid=True), nullable=True
    )

    # ── Identity ──────────────────────────────────────────────────────────
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    interaction_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # ── Display ───────────────────────────────────────────────────────────
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # ── Payload ───────────────────────────────────────────────────────────
    schema_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    response_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # ── Audit metadata (not shown to user) ────────────────────────────────
    context_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # ── Lifecycle ─────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="waiting_input"
    )
    resume_mode: Mapped[str] = mapped_column(String(50), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<GSageInteraction id={self.id} type={self.interaction_type}"
            f" status={self.status} tool={self.tool_name}>"
        )
