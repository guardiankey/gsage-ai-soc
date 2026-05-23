"""Curator — SQLAlchemy 2.0 ORM models.

Tables:
    curator_collections — reputation list definitions
    curator_items       — individual list entries
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import CIDR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ── Collection status / type constants ───────────────────────────────────────

COLLECTION_STATUSES = ("idle", "waiting", "processing")
COLLECTION_TYPES = (
    "ip",
    "cidr",
    "domain",
    "url",
    "domain_regex",
    "file_hash_md5",
    "file_hash_sha1",
    "file_hash_sha256",
    "email",
    "asn",
    "ja3",
    "ja4",
)
ITEM_TYPES = ("blocklist", "allowlist", "suspected")


def _make_slug(short_description: str, subtype: str | None, col_type: str) -> str:
    """Generate a slug from collection fields.

    Example: short_description='Email Senders', subtype='smtp_servers', type='ip'
    → 'email_senders_smtp_servers_ip'
    """
    parts = [short_description]
    if subtype:
        parts.append(subtype)
    parts.append(col_type)
    slug = "_".join(parts)
    slug = slug.lower()
    slug = re.sub(r"[^a-z0-9_]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug


# ── Models ────────────────────────────────────────────────────────────────────


class Collection(Base):
    __tablename__ = "curator_collections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    short_description: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    subtype: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # When False, the collection is hidden from the public /data/ HTTP endpoints
    # and its dump is skipped. The collection remains fully usable through the
    # authenticated admin API (/a/...), so the agent/tools can keep populating
    # it privately. Orthogonal to `active`: an unpublished collection can still
    # be active (queryable internally) but not exposed to HTTP consumers.
    published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="idle")

    items: Mapped[list[Item]] = relationship(
        "Item", back_populates="collection", cascade="all, delete-orphan"
    )

    def touch(self) -> None:
        self.updated_at = datetime.now(tz=timezone.utc)


class Item(Base):
    __tablename__ = "curator_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("curator_collections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # cidr column for ip/cidr types; value for everything else
    cidr: Mapped[str | None] = mapped_column(CIDR, nullable=True)
    value: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    public_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # blocklist/allowlist/suspected
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # Set when an item that had been soft-deleted is re-added; preserves original
    # created_at so the differential history of the original add is not rewritten.
    re_added_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Soft-delete timestamp. NULL = active. Items are physically purged
    # by the curator background loop after CURATOR_DIFF_RETENTION_DAYS.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    collection: Mapped[Collection] = relationship("Collection", back_populates="items")

    __table_args__ = (
        # Unique entries per collection — one constraint per column since CIDR/value are mutually exclusive
        UniqueConstraint("collection_id", "cidr", "type", name="uq_item_collection_cidr_type"),
        UniqueConstraint("collection_id", "value", "type", name="uq_item_collection_value_type"),
        Index("ix_curator_items_expire_at", "expire_at"),
        Index("ix_curator_items_deleted_at", "deleted_at"),
        Index("ix_curator_items_re_added_at", "re_added_at"),
    )
