"""gSage AI — BaseAuthProvider, AuthResult, AuthIdentity.

Each authentication provider implements BaseAuthProvider.  The pluggable
chain is configured per-organisation in GSageOrganization.auth_providers
(ordered list of provider names, e.g. ``["ldap", "local"]``).
"""

from __future__ import annotations

import os
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class AuthErrorType(str, Enum):
    """Classification of authentication failures.

    Used by the chain runner to decide whether to attempt the next provider.
    """

    # User recognised by the provider but credentials were wrong → STOP
    INVALID_CREDENTIALS = "invalid_credentials"
    # User recognised but account is blocked → STOP
    ACCOUNT_LOCKED = "account_locked"
    # User recognised but account is disabled → STOP
    ACCOUNT_DISABLED = "account_disabled"
    # User recognised but password has expired → STOP (return result with flag)
    PASSWORD_EXPIRED = "password_expired"

    # User not found in this provider's directory → try NEXT
    USER_NOT_FOUND = "user_not_found"
    # Provider is unreachable (network error, timeout) → try NEXT
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    # Provider is misconfigured (missing required fields) → try NEXT, log error
    CONFIGURATION_ERROR = "configuration_error"


# ---------------------------------------------------------------------------
# Data structures returned by providers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthIdentity:
    """Normalised identity returned by any successful authentication.

    Fields map directly to GSageUser columns; if the user does not yet
    exist in the database, ``user_sync.upsert_external_user`` creates it
    from this data.
    """

    email: str
    full_name: str
    external_id: Optional[str] = None   # objectGUID (AD), sub (OIDC), etc.
    avatar_url: Optional[str] = None
    phone: Optional[str] = None


@dataclass
class AuthResult:
    """Outcome of a single authentication attempt."""

    success: bool
    identity: Optional[AuthIdentity] = None
    # External group names/DNs to be mapped to local GSageGroups
    groups: list[str] = field(default_factory=list)
    error_type: Optional[AuthErrorType] = None
    error_message: Optional[str] = None
    # True when the provider signals that the user must change their password.
    # The issued JWT will carry the "pwd_change_required" claim; the client
    # is expected to prompt for a password change before proceeding.
    must_change_password: bool = False
    # Provider-specific extra data (department, title, employee_id, …)
    extra_claims: dict = field(default_factory=dict)
    # Filled by the registry after a successful authentication
    provider_name: str = ""

    @property
    def should_stop_chain(self) -> bool:
        """Return True when the chain must NOT attempt the next provider.

        A successful result always stops the chain.  Failures stop only when
        the provider positively identified the user and rejected them (wrong
        password, locked account, etc.).  Failures caused by the user not
        existing in this provider or by the provider being unavailable allow
        the chain to continue to the next provider.
        """
        if self.success:
            return True
        return self.error_type in (
            AuthErrorType.INVALID_CREDENTIALS,
            AuthErrorType.ACCOUNT_LOCKED,
            AuthErrorType.ACCOUNT_DISABLED,
            AuthErrorType.PASSWORD_EXPIRED,
        )


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class BaseAuthProvider(ABC):
    """Abstract base for all authentication providers.

    Subclasses must set the ``name`` and ``display_name`` class variables and
    implement ``authenticate``.  Config follows the same 3-layer resolution
    used by BaseTool:

        config_defaults  <  AUTH_{NAME}__{FIELD} env vars  <  DB per-org config

    The DB/per-org layer is merged by the registry before calling
    ``authenticate`` — providers always receive a fully-merged config dict.
    """

    name: ClassVar[str]
    display_name: ClassVar[str]

    # ── 3-layer config (mirrors BaseTool pattern) ────────────────────────
    config_schema: ClassVar[Optional[dict]] = None
    config_defaults: ClassVar[dict] = {}

    # Internal cache for env defaults (populated on first call)
    _env_defaults_cache: Optional[dict] = None

    @abstractmethod
    async def authenticate(
        self,
        username: str,
        password: str,
        config: dict,
    ) -> AuthResult:
        """Authenticate *username* / *password* against this provider.

        Parameters
        ----------
        username:
            The login identifier submitted by the user.  May be an email
            address, a SAMAccountName, a UPN — depends on what the provider
            accepts.
        password:
            Plain-text password.
        config:
            Fully-merged configuration (defaults < env vars < DB per-org).

        Returns
        -------
        AuthResult
            ``success=True`` with a populated ``identity`` on success;
            ``success=False`` with ``error_type`` set on failure.
        """

    async def healthcheck(self, config: dict) -> bool:
        """Verify provider connectivity (e.g. LDAP bind test).

        Returns True by default.  Override to implement a real check.
        """
        return True

    async def change_password(
        self,
        username: str,
        old_password: str,
        new_password: str,
        config: dict,
    ) -> bool:
        """Write-back password change to the external provider.

        Raises NotImplementedError when the provider does not support it.
        """
        raise NotImplementedError(f"{self.name} does not support password changes")

    # ── Environment defaults (same logic as BaseTool._load_env_defaults) ─

    def _load_env_defaults(self) -> dict:
        """Read AUTH_{NAME}__{FIELD} env vars and return as a config dict.

        Results are cached on the instance (providers are typically singletons).
        Sensitive fields are read normally (env vars are admin-controlled).
        """
        if self._env_defaults_cache is not None:
            return self._env_defaults_cache

        prefix = f"AUTH_{self.name.upper()}__"
        result: dict = {}

        for key, raw_value in os.environ.items():
            if not key.startswith(prefix):
                continue
            field_name = key[len(prefix):].lower()
            result[field_name] = self._coerce_env_value(field_name, raw_value)

        self._env_defaults_cache = result
        return result

    def _coerce_env_value(self, field: str, raw: str) -> Any:
        """Coerce a string env value to the appropriate Python type.

        Type resolution order:
        1. ``config_schema`` (both flat-dict and JSON-Schema ``properties``).
        2. Type of the matching ``config_defaults`` value.
        3. Falls back to string.
        """
        schema = self.config_schema or {}
        field_type: Optional[str] = None

        if "properties" in schema:
            field_type = schema["properties"].get(field, {}).get("type")
        else:
            raw_info = schema.get(field)
            if isinstance(raw_info, dict):
                field_type = raw_info.get("type")

        if field_type is None and field in self.config_defaults:
            default_val = self.config_defaults[field]
            if isinstance(default_val, bool):
                field_type = "boolean"
            elif isinstance(default_val, int):
                field_type = "integer"
            elif isinstance(default_val, float):
                field_type = "number"

        try:
            if field_type == "boolean":
                return raw.lower() in ("true", "1", "yes", "on")
            if field_type == "integer":
                return int(raw)
            if field_type == "number":
                return float(raw)
        except (ValueError, TypeError):
            logger.warning(
                "AuthProvider %s: could not coerce env var field '%s' value %r "
                "to %s — keeping as string",
                self.name, field, raw, field_type,
            )
        return raw
