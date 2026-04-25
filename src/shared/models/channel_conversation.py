"""gSage AI — Generic channel conversation model.

Tracks the ongoing conversation between one user and the AI on a specific
channel chat (e.g. a Telegram private chat, a Discord DM).

One row per (org_id, channel, channel_chat_id, user_id) — the unique constraint
ensures we always reuse the same GSageTenantSession for that conversation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional
import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.tenant_session import GSageTenantSession
    from src.shared.models.user import GSageUser


class GSageChannelConversation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-user, per-chat-id conversation tracked across channel messages.

    Wraps a GSageTenantSession so the agent keeps full conversational memory.
    """

    __tablename__ = "gsage_channel_conversations"

    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "channel",
            "channel_chat_id",
            "user_id",
            name="uq_channel_conversation_org_channel_chat_user",
        ),
    )

    # Tenant isolation
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Channel identification
    channel: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Channel name: telegram | discord | slack | whatsapp",
    )
    channel_chat_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Channel-native chat/conversation identifier",
    )

    # Linked agent session (created once, reused for memory continuity)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_tenant_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Message counter
    message_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Total messages exchanged in this conversation",
    )

    # Relationships
    organization: Mapped[GSageOrganization] = relationship("GSageOrganization")
    user: Mapped[GSageUser] = relationship("GSageUser")
    session: Mapped[Optional[GSageTenantSession]] = relationship("GSageTenantSession")

    def __repr__(self) -> str:
        return (
            f"<GSageChannelConversation(id={self.id}, channel={self.channel}, "
            f"chat_id={self.channel_chat_id}, user_id={self.user_id})>"
        )
