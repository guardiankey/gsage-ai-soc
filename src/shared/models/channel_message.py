"""gSage AI — Generic channel message model.

Covers all non-email messaging channels: Telegram, Discord, Slack, WhatsApp, etc.
Each inbound message and the corresponding outbound reply are stored here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional
import uuid
import enum

from sqlalchemy import Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.tenant_session import GSageTenantSession
    from src.shared.models.user import GSageUser


class GSageChannelDirection(str, enum.Enum):
    """Message direction relative to the system."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class GSageChannelStatus(str, enum.Enum):
    """Message processing status."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class GSageChannelMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Generic channel message (inbound and outbound).

    Stores all messages processed by channel workers (Telegram, Discord, etc.).
    """

    __tablename__ = "gsage_channel_messages"

    __table_args__ = (
        # Idempotency: one row per (channel, message_id_on_that_channel)
        Index(
            "ix_gsage_channel_messages_channel_msg_id",
            "channel",
            "channel_message_id",
            unique=True,
        ),
    )

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
        comment="Resolved GSageUser (inbound) or None if unknown sender",
    )

    # Channel identification
    channel: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="Channel name: telegram | discord | slack | whatsapp",
    )
    channel_chat_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Channel-native chat/conversation identifier (e.g. Telegram chat_id)",
    )
    channel_message_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Channel-native message identifier for idempotency",
    )

    # Direction and status
    direction: Mapped[GSageChannelDirection] = mapped_column(
        Enum(GSageChannelDirection, native_enum=False),
        nullable=False,
    )
    status: Mapped[GSageChannelStatus] = mapped_column(
        Enum(GSageChannelStatus, native_enum=False),
        nullable=False,
        default=GSageChannelStatus.PENDING,
    )

    # Message content
    text: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Plain text content of the message",
    )

    # Link to tenant session (conversation)
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
        comment="Error detail when status=failed",
    )

    # Relationships
    organization: Mapped[GSageOrganization] = relationship("GSageOrganization")
    user: Mapped[Optional[GSageUser]] = relationship("GSageUser")
    session: Mapped[Optional[GSageTenantSession]] = relationship("GSageTenantSession")

    def __repr__(self) -> str:
        return (
            f"<GSageChannelMessage(id={self.id}, channel={self.channel}, "
            f"channel_message_id={self.channel_message_id}, direction={self.direction})>"
        )
