"""gSage AI — LocalAuthProvider (bcrypt, database-backed).

This provider wraps the existing email/password authentication that
GSageUsers with ``auth_provider = "local"`` use.  It is always the
last fallback in the default chain (``["local"]``).

No external connectivity is required — credentials are verified against the
``password_hash`` column of ``gsage_users`` in PostgreSQL.

The provider has no configurable fields; ``config_schema`` and
``config_defaults`` are intentionally empty.
"""

from __future__ import annotations

import logging

from src.shared.auth.base import (
    AuthErrorType,
    AuthIdentity,
    AuthResult,
    BaseAuthProvider,
)

logger = logging.getLogger(__name__)


class LocalAuthProvider(BaseAuthProvider):
    """Authenticate against the gSage local user database (bcrypt)."""

    name = "local"
    display_name = "Local (bcrypt / database)"

    # No external configuration required
    config_schema = None
    config_defaults = {}

    async def authenticate(
        self,
        username: str,
        password: str,
        config: dict,
    ) -> AuthResult:
        """Validate *username* (email) and *password* against the local DB.

        The database lookup is intentionally deferred so that this provider
        can be instantiated without an active DB session — the session is
        injected by the login route which calls this provider only after
        loading it from the registry.

        Important
        ---------
        This method does NOT have direct access to the database — the login
        route in ``auth.py`` performs the lookup and delegates to this
        provider only for the password verification step.  The helper method
        ``verify_credentials`` below is called directly by the route for the
        local case; the registry chain calls ``authenticate`` for all other
        providers.

        When the login route uses the registry chain for "local", it injects
        the user's ``password_hash`` in the config dict under the key
        ``_password_hash``.  This avoids an extra DB round-trip.
        """
        # The login route resolves the user first and injects the hash.
        # If not injected, we cannot verify (return USER_NOT_FOUND so the
        # chain can fall through — although in practice "local" is last).
        password_hash: str | None = config.get("_password_hash")
        email: str = config.get("_email", username)
        full_name: str = config.get("_full_name", "")

        if password_hash is None:
            return AuthResult(
                success=False,
                error_type=AuthErrorType.USER_NOT_FOUND,
                error_message="User not found in local database",
            )

        from src.shared.security.auth import verify_password

        if not verify_password(password, password_hash):
            return AuthResult(
                success=False,
                error_type=AuthErrorType.INVALID_CREDENTIALS,
                error_message="Incorrect email or password",
            )

        return AuthResult(
            success=True,
            identity=AuthIdentity(
                email=email,
                full_name=full_name,
            ),
            provider_name="local",
        )
