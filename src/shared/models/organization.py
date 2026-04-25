"""gSage AI — Organization model."""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from src.shared.security.encryption import get_encryption

if TYPE_CHECKING:
    from src.shared.models.agent_run import GSageAgentRun
    from src.shared.models.api_key import GSageAPIKey
    from src.shared.models.department import GSageDepartment
    from src.shared.models.email_account import GSageEmailAccount
    from src.shared.models.group import GSageGroup
    from src.shared.models.interface_profile import GSageInterfaceProfile
    from src.shared.models.tenant_session import GSageTenantSession
    from src.shared.models.user_organization import GSageUserOrganization


def _default_llm_provider() -> str:
    """Return the system LLM provider from settings (used as column default)."""
    from src.shared.config.settings import get_settings
    return get_settings().llm_provider


class GSageOrganization(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Organization (tenant) model.

    Each organization is a separate tenant with complete data isolation.
    """

    __tablename__ = "gsage_organizations"

    # Basic info
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Agent configuration (Phase 5)
    system_prompt: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Custom agent persona per organization (max 5000 chars)",
    )
    default_maker_model: Mapped[str] = mapped_column(
        String(100),
        default="llama3.1:8b",
        nullable=False,
    )
    default_reviewer_model: Mapped[str] = mapped_column(
        String(100),
        default="llama3.1:8b",
        nullable=False,
    )
    agent_timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        default=120,
        nullable=False,
        comment="Global agent execution timeout (max: 300)",
    )
    max_context_tokens: Mapped[int] = mapped_column(
        Integer,
        default=6000,
        nullable=False,
        comment="Max context window size for LLM",
    )

    # LLM provider selection (org-level override; falls back to Settings.llm_provider)
    llm_provider: Mapped[str] = mapped_column(
        String(20),
        default=_default_llm_provider,
        nullable=False,
        comment="LLM backend provider: ollama | openai | deepseek | anthropic | gemini",
    )

    # Encrypted API key for the selected LLM provider (org-specific, optional)
    _llm_api_key_encrypted: Mapped[Optional[bytes]] = mapped_column(
        "llm_api_key_encrypted",
        LargeBinary,
        nullable=True,
        comment="AES-256-GCM encrypted API key for the org LLM provider",
    )

    # Per-org SMTP config override (AES-256-GCM encrypted JSON blob, optional)
    # Schema: {host, port, username, password, use_tls, from_email, from_name, default_format}
    _smtp_config_encrypted: Mapped[Optional[bytes]] = mapped_column(
        "smtp_config_encrypted",
        LargeBinary,
        nullable=True,
        comment="AES-256-GCM encrypted SMTP config override (JSON)",
    )

    # Auth providers — ordered list of provider names for login chain e.g. ["ldap", "local"]
    _auth_providers_json: Mapped[str] = mapped_column(
        "auth_providers",
        String(500),
        default='["local"]',
        nullable=False,
        comment="Ordered JSON list of auth provider names for login chain",
    )

    # Per-org auth provider config override (AES-256-GCM encrypted JSON blob, optional)
    # Schema: {"ldap": {server_url, bind_dn, ...}, "saml": {...}}
    _auth_config_encrypted: Mapped[Optional[bytes]] = mapped_column(
        "auth_config_encrypted",
        LargeBinary,
        nullable=True,
        comment="AES-256-GCM encrypted per-provider auth config overrides (JSON)",
    )

    # Relationships
    memberships: Mapped[List[GSageUserOrganization]] = relationship(
        "GSageUserOrganization",
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    groups: Mapped[List[GSageGroup]] = relationship(
        "GSageGroup",
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    api_keys: Mapped[List[GSageAPIKey]] = relationship(
        "GSageAPIKey",
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    email_accounts: Mapped[List[GSageEmailAccount]] = relationship(
        "GSageEmailAccount",
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    tenant_sessions: Mapped[List[GSageTenantSession]] = relationship(
        "GSageTenantSession",
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    agent_runs: Mapped[List[GSageAgentRun]] = relationship(
        "GSageAgentRun",
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    interface_profiles: Mapped[List["GSageInterfaceProfile"]] = relationship(
        "GSageInterfaceProfile",
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    departments: Mapped[List["GSageDepartment"]] = relationship(
        "GSageDepartment",
        back_populates="organization",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="GSageDepartment.name",
    )

    @property
    def llm_api_key(self) -> Optional[str]:
        """Decrypt and return the org's LLM provider API key."""
        if not self._llm_api_key_encrypted:
            return None
        return get_encryption().decrypt(self._llm_api_key_encrypted)

    @llm_api_key.setter
    def llm_api_key(self, value: Optional[str]) -> None:
        """Encrypt and store the org's LLM provider API key."""
        if value:
            self._llm_api_key_encrypted = get_encryption().encrypt(value)
        else:
            self._llm_api_key_encrypted = None

    @property
    def smtp_config(self) -> Optional[dict]:
        """Decrypt and return the org's SMTP config override dict."""
        if not self._smtp_config_encrypted:
            return None
        import json
        return json.loads(get_encryption().decrypt(self._smtp_config_encrypted))

    @smtp_config.setter
    def smtp_config(self, value: Optional[dict]) -> None:
        """Encrypt and store the org's SMTP config override dict."""
        if value:
            import json
            self._smtp_config_encrypted = get_encryption().encrypt(json.dumps(value))
        else:
            self._smtp_config_encrypted = None

    @property
    def auth_providers(self) -> list[str]:
        """Return the ordered list of auth provider names for the login chain."""
        import json
        try:
            return json.loads(self._auth_providers_json)
        except Exception:
            return ["local"]

    @auth_providers.setter
    def auth_providers(self, value: list[str]) -> None:
        import json
        self._auth_providers_json = json.dumps(value)

    @property
    def auth_config(self) -> dict:
        """Decrypt and return the org's per-provider auth config overrides."""
        if not self._auth_config_encrypted:
            return {}
        import json
        return json.loads(get_encryption().decrypt(self._auth_config_encrypted))

    @auth_config.setter
    def auth_config(self, value: Optional[dict]) -> None:
        """Encrypt and store the org's per-provider auth config overrides."""
        if value:
            import json
            self._auth_config_encrypted = get_encryption().encrypt(json.dumps(value))
        else:
            self._auth_config_encrypted = None

    def __repr__(self) -> str:
        return f"<GSageOrganization(id={self.id}, name={self.name}, slug={self.slug})>"
