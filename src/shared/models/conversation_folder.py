"""gSage AI — ConversationFolder model.

A folder groups conversations (GSageTenantSession) into a single-level
hierarchy. Folders are private to a user within an organization (scoped by
org_id + user_id). Archiving a folder cascades to its conversations.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, List

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.tenant_session import GSageTenantSession


class GSageConversationFolder(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A single-level folder grouping a user's conversations.

    Folders are private to the owning user within an organization. They are
    used purely for UI organization of the conversation sidebar.
    """

    __tablename__ = "gsage_conversation_folders"

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
        comment="Owner of the folder. Folders are private to a single user.",
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable folder name.",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="Archive flag. False = archived (hidden by default).",
    )

    # Relationships
    sessions: Mapped[List["GSageTenantSession"]] = relationship(
        "GSageTenantSession",
        back_populates="folder",
    )

    def __repr__(self) -> str:
        return (
            f"<GSageConversationFolder("
            f"id={self.id}, name={self.name!r}, org_id={self.org_id}, user_id={self.user_id})>"
        )
