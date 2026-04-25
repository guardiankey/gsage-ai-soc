"""gSage AI — TrustedDevice model for OTP remember-device flow."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.user import GSageUser


class GSageTrustedDevice(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Stores remembered devices for OTP skip during the configured TTL.

    When a user chooses "remember this device" during OTP verification, a
    device token is issued and its SHA-256 hash is stored here. Subsequent
    logins from the same device skip the OTP step until the record expires.
    """

    __tablename__ = "gsage_trusted_devices"

    user_id: Mapped[object] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # SHA-256 hex-digest of the raw device token (never store the raw token)
    device_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
        comment="SHA-256 hex-digest of the device token",
    )

    user_agent: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="User-Agent string at the time of trust grant",
    )

    ip_address: Mapped[str | None] = mapped_column(
        String(45),
        nullable=True,
        comment="IP address at the time of trust grant (IPv4 or IPv6)",
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="UTC datetime after which this trust record is considered expired",
    )

    # Relationship
    user: Mapped[GSageUser] = relationship(
        "GSageUser",
        back_populates="trusted_devices",
    )

    def __repr__(self) -> str:
        return f"<GSageTrustedDevice(id={self.id}, user_id={self.user_id}, expires_at={self.expires_at})>"
