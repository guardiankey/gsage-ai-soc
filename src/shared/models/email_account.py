"""gSage AI — Email Account model (IMAP/SMTP per org)."""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional
import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from src.shared.security.encryption import get_encryption

if TYPE_CHECKING:
    from src.shared.models.email_message import GSageEmailMessage
    from src.shared.models.organization import GSageOrganization


class GSageEmailAccount(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Email account configuration (IMAP/SMTP) per organization.

    One email account = one organization (no shared mailboxes).
    """

    __tablename__ = "gsage_email_accounts"

    # Tenant isolation (one account per org)
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
        comment="Department this account is scoped to. NULL = shared across org.",
    )

    # Account info
    display_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Friendly name (e.g., 'SOC Mailbox')",
    )
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
        comment="Mailbox email address (also default From address)",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    # IMAP settings
    imap_host: Mapped[str] = mapped_column(String(255), nullable=False)
    imap_port: Mapped[int] = mapped_column(Integer, default=993, nullable=False)
    imap_use_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    imap_verify_ssl: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Verify TLS certificate on IMAP connection. Set False for self-signed certs.",
    )
    imap_username: Mapped[str] = mapped_column(String(255), nullable=False)
    _imap_password_encrypted: Mapped[Optional[bytes]] = mapped_column(
        "imap_password_encrypted",
        LargeBinary,
        nullable=True,
        comment="AES-256-GCM encrypted IMAP password (NULL for OAuth2 accounts)",
    )
    imap_folder: Mapped[str] = mapped_column(String(100), default="INBOX", nullable=False)
    imap_idle_supported: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether server supports IMAP IDLE",
    )

    # SMTP settings
    smtp_host: Mapped[str] = mapped_column(String(255), nullable=False)
    smtp_port: Mapped[int] = mapped_column(Integer, default=587, nullable=False)
    smtp_use_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    smtp_verify_ssl: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Verify TLS certificate on SMTP connection. Set False for self-signed certs.",
    )
    # Empty string = no authentication (e.g. relay on port 25)
    smtp_username: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    _smtp_password_encrypted: Mapped[Optional[bytes]] = mapped_column(
        "smtp_password_encrypted",
        LargeBinary,
        nullable=True,
        comment="AES-256-GCM encrypted SMTP password. NULL = unauthenticated relay.",
    )

    # Email formatting
    sender_name: Mapped[str] = mapped_column(
        String(255),
        default="SOC AI Assistant",
        nullable=False,
        comment="Display name in From header",
    )
    subject_prefix: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="Prefix for outbound subjects (e.g., '[SOC-AI]')",
    )
    reply_footer: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Footer appended to outbound emails",
    )

    # Behavior
    unknown_sender_folder: Mapped[str] = mapped_column(
        String(100),
        default="Unknown-Senders",
        nullable=False,
        comment="IMAP folder for unrecognized senders",
    )
    max_email_size_bytes: Mapped[int] = mapped_column(
        Integer,
        default=5242880,  # 5MB
        nullable=False,
    )
    polling_interval_seconds: Mapped[int] = mapped_column(
        Integer,
        default=60,
        nullable=False,
        comment="Fallback polling interval when IDLE not supported",
    )

    # Authentication method
    auth_method: Mapped[str] = mapped_column(
        String(20),
        default="basic",
        server_default="basic",
        nullable=False,
        comment="Authentication mode: 'basic' (LOGIN with password) or 'oauth2' (XOAUTH2 client-credentials)",
    )

    # OAuth2 (Microsoft 365 / Exchange Online client-credentials flow).
    # When auth_method='oauth2', these fields take precedence over the
    # *_password fields and the worker fetches an access token via the
    # /oauth2/v2.0/token endpoint.
    oauth_tenant_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Azure AD tenant ID (UUID) for OAuth2 token endpoint.",
    )
    oauth_client_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Azure AD app client_id (the registered application).",
    )
    _oauth_client_secret_encrypted: Mapped[Optional[bytes]] = mapped_column(
        "oauth_client_secret_encrypted",
        LargeBinary,
        nullable=True,
        comment="AES-256-GCM encrypted OAuth2 client_secret.",
    )
    oauth_token_endpoint: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment=(
            "Override OAuth2 token endpoint. Defaults to "
            "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token "
            "when NULL."
        ),
    )
    oauth_scope: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment=(
            "OAuth2 scope. Defaults to "
            "'https://outlook.office365.com/.default' when NULL."
        ),
    )

    # Relationships
    organization: Mapped[GSageOrganization] = relationship(
        "GSageOrganization",
        back_populates="email_accounts",
    )
    messages: Mapped[List[GSageEmailMessage]] = relationship(
        "GSageEmailMessage",
        back_populates="email_account",
        cascade="all, delete-orphan",
    )

    @property
    def imap_password(self) -> str:
        """Decrypt and return IMAP password, or empty string when not set (OAuth2)."""
        if not self._imap_password_encrypted:
            return ""
        return get_encryption().decrypt(self._imap_password_encrypted)

    @imap_password.setter
    def imap_password(self, value: str) -> None:
        """Encrypt and store IMAP password. Empty string clears the stored credential."""
        if not value:
            self._imap_password_encrypted = None
        else:
            self._imap_password_encrypted = get_encryption().encrypt(value)

    @property
    def smtp_password(self) -> str:
        """Decrypt and return SMTP password, or empty string for unauthenticated accounts."""
        if not self._smtp_password_encrypted:
            return ""
        return get_encryption().decrypt(self._smtp_password_encrypted)

    @smtp_password.setter
    def smtp_password(self, value: str) -> None:
        """Encrypt and store SMTP password. Empty string clears the stored credential."""
        if not value:
            self._smtp_password_encrypted = None
        else:
            self._smtp_password_encrypted = get_encryption().encrypt(value)

    @property
    def oauth_client_secret(self) -> str:
        """Decrypt and return OAuth2 client_secret, or empty string when not set."""
        if not self._oauth_client_secret_encrypted:
            return ""
        return get_encryption().decrypt(self._oauth_client_secret_encrypted)

    @oauth_client_secret.setter
    def oauth_client_secret(self, value: str) -> None:
        """Encrypt and store OAuth2 client_secret. Empty string clears it."""
        if not value:
            self._oauth_client_secret_encrypted = None
        else:
            self._oauth_client_secret_encrypted = get_encryption().encrypt(value)

    def __repr__(self) -> str:
        return f"<GSageEmailAccount(id={self.id}, email={self.email}, org_id={self.org_id})>"
