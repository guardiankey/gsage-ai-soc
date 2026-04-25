"""gSage AI — Dynamic DataStore models.

Two models:
- GSageDataStore     : named store definition with JSON Schema for validation
- GSageDataStoreRecord : individual records inside a store
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class GSageDataStore(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Named store definition owned by an org."""

    __tablename__ = "gsage_data_stores"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dept_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_departments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="Department that owns this DataStore. Always required.",
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("gsage_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # JSON Schema draft-07 for record validation. Empty object {} = no validation.
    schema: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # "private" | "shared"
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, default="shared")

    max_records: Mapped[int] = mapped_column(Integer, nullable=False, default=500)

    # Denormalized counter — kept in sync by the service layer.
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    # ── Relationships ────────────────────────────────────────────────────────
    records: Mapped[list["GSageDataStoreRecord"]] = relationship(
        "GSageDataStoreRecord",
        back_populates="datastore",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("dept_id", "name", name="uq_datastore_dept_name"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<GSageDataStore id={self.id} name={self.name!r} org_id={self.org_id}>"


class GSageDataStoreRecord(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Single record inside a GSageDataStore."""

    __tablename__ = "gsage_data_store_records"

    datastore_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gsage_data_stores.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Record payload — validated against store.schema on write.
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # ── Relationships ────────────────────────────────────────────────────────
    datastore: Mapped["GSageDataStore"] = relationship(
        "GSageDataStore",
        back_populates="records",
    )

    __table_args__ = (
        # GIN index for efficient JSONB containment queries (@>)
        Index("ix_datastore_record_data_gin", "data", postgresql_using="gin"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<GSageDataStoreRecord id={self.id} datastore_id={self.datastore_id}>"
