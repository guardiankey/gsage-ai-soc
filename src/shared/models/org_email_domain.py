"""gSage AI — Organization email domain mapping (for SSO email-based discovery)."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from src.shared.models.organization import GSageOrganization


class GSageOrgEmailDomain(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Maps an email domain (e.g. ``acme.com``) to a single organization.

    Used by the public auth lookup endpoint to discover which organization
    (and which SSO providers) own a given email address. Domains are
    globally unique: a domain can belong to at most one org.
    """

    __tablename__ = "gsage_org_email_domains"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gsage_organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    domain: Mapped[str] = mapped_column(
        String(253),
        nullable=False,
        unique=True,
        index=True,
        comment="Lowercased DNS domain name (RFC 1035 max length 253)",
    )

    organization: Mapped["GSageOrganization"] = relationship(
        "GSageOrganization",
        back_populates="email_domains",
    )

    @validates("domain")
    def _normalize_domain(self, _key: str, value: str) -> str:
        if not value:
            raise ValueError("domain must not be empty")
        return value.strip().lower()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<GSageOrgEmailDomain {self.domain} -> {self.org_id}>"
