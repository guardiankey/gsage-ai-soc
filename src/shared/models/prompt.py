"""gSage AI — Prompt Library models.

Multi-tenant prompt storage with hierarchical categories, per-user favorites,
and three-tier scoping (personal / department / organization).

Tables
------
- ``gsage_prompt_categories`` — hierarchical category tree (org-scoped, optional dept)
- ``gsage_prompts`` — prompt content with scope and visibility rules
- ``gsage_user_prompt_favorites`` — per-user favorite toggle (associative)
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.department import GSageDepartment
    from src.shared.models.organization import GSageOrganization
    from src.shared.models.user import GSageUser


# ---------------------------------------------------------------------------
# GSagePromptCategory — hierarchical category tree
# ---------------------------------------------------------------------------


class GSagePromptCategory(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Hierarchical prompt category, scoped to an org and optionally a department.

    - ``dept_id IS NULL`` → org-level category (visible to entire org).
    - ``dept_id = <uuid>`` → department-level category (visible only within that dept).
    """

    __tablename__ = "gsage_prompt_categories"

    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dept_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("gsage_departments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("gsage_prompt_categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # -- relationships -------------------------------------------------------
    organization: Mapped[GSageOrganization] = relationship()
    department: Mapped[Optional[GSageDepartment]] = relationship()
    parent: Mapped[Optional[GSagePromptCategory]] = relationship(
        remote_side="GSagePromptCategory.id",
        back_populates="children",
    )
    children: Mapped[List[GSagePromptCategory]] = relationship(
        back_populates="parent",
        order_by="GSagePromptCategory.sort_order, GSagePromptCategory.name",
    )
    prompts: Mapped[List[GSagePrompt]] = relationship(
        back_populates="category",
        order_by="GSagePrompt.title",
    )

    __table_args__ = (
        Index("ix_gsage_prompt_cat_org_dept", "org_id", "dept_id"),
    )

    def __repr__(self) -> str:
        return f"<GSagePromptCategory {self.name!r} (org={self.org_id})>"


# ---------------------------------------------------------------------------
# GSagePrompt — prompt content
# ---------------------------------------------------------------------------


class GSagePrompt(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A reusable prompt template.

    Visibility is governed by ``scope`` and ``dept_id``:

    - ``personal`` — only the creator sees it.
    - ``department`` — all members of ``dept_id`` see it.
    - ``organization`` — all org members see it.
    """

    __tablename__ = "gsage_prompts"

    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dept_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("gsage_departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gsage_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("gsage_prompt_categories.id", ondelete="SET NULL"),
        nullable=True,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    scope: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="personal",
        comment="personal | department | organization",
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # -- relationships -------------------------------------------------------
    organization: Mapped[GSageOrganization] = relationship()
    department: Mapped[Optional[GSageDepartment]] = relationship()
    creator: Mapped[GSageUser] = relationship(foreign_keys=[created_by])
    category: Mapped[Optional[GSagePromptCategory]] = relationship(back_populates="prompts")
    favorited_by: Mapped[List[GSageUser]] = relationship(
        secondary="gsage_user_prompt_favorites",
        back_populates="favorite_prompts",
    )

    __table_args__ = (
        Index("ix_gsage_prompts_org_scope", "org_id", "scope"),
    )

    def __repr__(self) -> str:
        return f"<GSagePrompt {self.title!r} (scope={self.scope})>"


# ---------------------------------------------------------------------------
# GSageUserPromptFavorite — per-user favorite toggle (associative table)
# ---------------------------------------------------------------------------


class GSageUserPromptFavorite(Base, TimestampMixin):
    """Associative table for per-user prompt favorites.

    Existence of a row = favorited; absence = not favorited.
    Both columns form a composite primary key.
    """

    __tablename__ = "gsage_user_prompt_favorites"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gsage_users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    prompt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("gsage_prompts.id", ondelete="CASCADE"),
        primary_key=True,
    )

    def __repr__(self) -> str:
        return f"<GSageUserPromptFavorite user={self.user_id} prompt={self.prompt_id}>"
