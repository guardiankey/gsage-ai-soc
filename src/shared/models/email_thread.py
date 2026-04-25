"""gSage AI — Email Thread model."""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional
import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.email_message import GSageEmailMessage
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.tenant_session import GSageTenantSession
    from src.shared.models.user import GSageUser


class GSageEmailThread(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Email thread tracking for conversation continuity.

    Links related emails together and to a conversation.
    """

    __tablename__ = "gsage_email_threads"

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

    # Thread identification
    thread_subject: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Normalized subject (without Re:/Fwd:)",
    )

    # Link to tenant session
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_tenant_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Links thread to an ongoing tenant session",
    )

    # Thread metadata
    first_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_email_messages.id", ondelete="SET NULL", use_alter=True, name="fk_thread_first_message"),
        nullable=True,
    )
    last_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_email_messages.id", ondelete="SET NULL", use_alter=True, name="fk_thread_last_message"),
        nullable=True,
    )
    message_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    # Relationships
    organization: Mapped[GSageOrganization] = relationship("GSageOrganization")
    user: Mapped[GSageUser] = relationship("GSageUser")
    session: Mapped[Optional["GSageTenantSession"]] = relationship(
        "GSageTenantSession",
        back_populates="email_threads",
    )
    messages: Mapped[List[GSageEmailMessage]] = relationship(
        "GSageEmailMessage",
        back_populates="thread",
        foreign_keys="[GSageEmailMessage.thread_id]",
    )

    def __repr__(self) -> str:
        return f"<GSageEmailThread(id={self.id}, subject={self.thread_subject[:50]}, session_id={self.session_id})>"
