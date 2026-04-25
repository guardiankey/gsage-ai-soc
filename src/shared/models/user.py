"""gSage AI — User model."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, LargeBinary, String, Table, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from src.shared.security.encryption import get_encryption

if TYPE_CHECKING:
    from src.shared.models.api_key import GSageAPIKey
    from src.shared.models.group import GSageGroup
    from src.shared.models.tenant_session import GSageTenantSession
    from src.shared.models.trusted_device import GSageTrustedDevice
    from src.shared.models.user_department import GSageUserDepartment
    from src.shared.models.user_organization import GSageUserOrganization


# Association table for many-to-many: GSageUser <-> GSageGroup
gsage_user_groups = Table(
    "gsage_user_groups",
    Base.metadata,
    Column("user_id", UUID(as_uuid=True), ForeignKey("gsage_users.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", UUID(as_uuid=True), ForeignKey("gsage_groups.id", ondelete="CASCADE"), primary_key=True),
)


class GSageUser(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """User model — globally unique by email, belongs to orgs via GSageUserOrganization.

    A user can be a member of multiple organizations. Role and permissions
    per organization are stored in GSageUserOrganization, not here.
    """

    __tablename__ = "gsage_users"

    # Authentication
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
        comment="Primary email — globally unique across all orgs (used for login)",
    )
    password_hash: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="bcrypt hash — NULL for users provisioned by external auth providers",
    )

    # Auth provider tracking
    auth_provider: Mapped[str] = mapped_column(
        String(50),
        default="local",
        nullable=False,
        comment="Name of the auth provider that last authenticated this user",
    )
    external_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        comment="Stable external identifier from the auth provider (e.g. LDAP objectGUID)",
    )

    # Profile
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Email addresses (Phase 7 — email identification)
    secondary_emails: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Alternative email addresses, one per line (newline-separated, max 5)",
    )

    # Per-user AI instructions injected into every agent system prompt
    ai_instructions: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="User preferences / custom instructions for the AI assistant (e.g. language, tone, context)",
    )

    # Telegram integration
    telegram_id: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        index=True,
        comment="Telegram numeric user ID (as string) for bot sender resolution",
    )

    # OTP / TOTP (RFC 6238)
    _otp_secret_encrypted: Mapped[Optional[bytes]] = mapped_column(
        "otp_secret_encrypted",
        LargeBinary,
        nullable=True,
        comment="AES-256-GCM encrypted TOTP secret (base32)",
    )
    otp_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
        comment="Whether OTP is active for this user (confirmed enrollment)",
    )
    otp_confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when OTP enrollment was confirmed",
    )
    _otp_backup_codes_encrypted: Mapped[Optional[bytes]] = mapped_column(
        "otp_backup_codes_encrypted",
        LargeBinary,
        nullable=True,
        comment="AES-256-GCM encrypted JSON array of bcrypt-hashed backup codes",
    )

    # Relationships
    memberships: Mapped[List[GSageUserOrganization]] = relationship(
        "GSageUserOrganization",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    groups: Mapped[List[GSageGroup]] = relationship(
        "GSageGroup",
        secondary=gsage_user_groups,
        back_populates="users",
    )
    tenant_sessions: Mapped[List[GSageTenantSession]] = relationship(
        "GSageTenantSession",
        back_populates="user",
    )
    api_keys: Mapped[List[GSageAPIKey]] = relationship(
        "GSageAPIKey",
        foreign_keys="GSageAPIKey.user_id",
        back_populates="user",
    )
    trusted_devices: Mapped[List[GSageTrustedDevice]] = relationship(
        "GSageTrustedDevice",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    department_memberships: Mapped[List["GSageUserDepartment"]] = relationship(
        "GSageUserDepartment",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # --- OTP properties ---

    @property
    def otp_secret(self) -> Optional[str]:
        """Decrypt and return the TOTP secret (base32 string)."""
        if not self._otp_secret_encrypted:
            return None
        return get_encryption().decrypt(self._otp_secret_encrypted)

    @otp_secret.setter
    def otp_secret(self, value: Optional[str]) -> None:
        """Encrypt and store the TOTP secret."""
        if value:
            self._otp_secret_encrypted = get_encryption().encrypt(value)
        else:
            self._otp_secret_encrypted = None

    @property
    def otp_backup_codes(self) -> Optional[list[str]]:
        """Decrypt and return the list of bcrypt-hashed backup codes."""
        if not self._otp_backup_codes_encrypted:
            return None
        return json.loads(get_encryption().decrypt(self._otp_backup_codes_encrypted))

    @otp_backup_codes.setter
    def otp_backup_codes(self, value: Optional[list[str]]) -> None:
        """Encrypt and store the list of bcrypt-hashed backup codes."""
        if value is not None:
            self._otp_backup_codes_encrypted = get_encryption().encrypt(json.dumps(value))
        else:
            self._otp_backup_codes_encrypted = None

    def __repr__(self) -> str:
        return f"<GSageUser(id={self.id}, email={self.email})>"
