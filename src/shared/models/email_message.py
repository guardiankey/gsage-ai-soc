"""gSage AI — Email Message model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional
import uuid
import enum

from sqlalchemy import Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.email_account import GSageEmailAccount
    from src.shared.models.email_thread import GSageEmailThread
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.tenant_session import GSageTenantSession
    from src.shared.models.user import GSageUser


class GSageEmailDirection(str, enum.Enum):
    """Email direction."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class GSageEmailStatus(str, enum.Enum):
    """Email processing status."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class GSageEmailMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Email message model (inbound and outbound).

    Stores all emails processed by the system.
    """

    __tablename__ = "gsage_email_messages"

    # Tenant isolation
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="User who sent (inbound) or recipient (outbound)",
    )

    # Email account used
    email_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_email_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Email headers
    message_id: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        unique=True,
        index=True,
        comment="IMAP Message-ID header (globally unique)",
    )
    in_reply_to: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment="In-Reply-To header for threading",
    )
    references: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="References header (space-separated Message-IDs)",
    )

    # Thread relationship
    thread_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_email_threads.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Direction and status
    direction: Mapped[GSageEmailDirection] = mapped_column(
        Enum(GSageEmailDirection, native_enum=False),
        nullable=False,
    )
    status: Mapped[GSageEmailStatus] = mapped_column(
        Enum(GSageEmailStatus, native_enum=False),
        nullable=False,
        default=GSageEmailStatus.PENDING,
    )

    # Email content
    from_addr: Mapped[str] = mapped_column(String(255), nullable=False)
    to_addr: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body_text: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Plain text body",
    )
    body_html: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="HTML body",
    )

    # Link to tenant session
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_tenant_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Error tracking
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Error message if status=failed",
    )

    # Relationships
    organization: Mapped[GSageOrganization] = relationship("GSageOrganization")
    user: Mapped[Optional[GSageUser]] = relationship("GSageUser")
    email_account: Mapped[GSageEmailAccount] = relationship(
        "GSageEmailAccount",
        back_populates="messages",
    )
    thread: Mapped[Optional[GSageEmailThread]] = relationship(
        "GSageEmailThread",
        back_populates="messages",
        foreign_keys=[thread_id],
    )
    conversation: Mapped[Optional["GSageTenantSession"]] = relationship(
        "GSageTenantSession",
    )

    def __repr__(self) -> str:
        return f"<GSageEmailMessage(id={self.id}, message_id={self.message_id}, direction={self.direction})>"
