"""custom_code/auth_backends/example_backend.py — Minimal custom auth backend.

Copy this file, rename the class and ``name`` ClassVar, and implement
``authenticate()``.  The backend will be auto-discovered by the
AuthProviderRegistry at startup as long as:
  - It is a concrete (non-abstract) subclass of ``BaseAuthProvider``.
  - It has a unique lowercase ``name`` ClassVar.

Config defaults
---------------
Declare hard-coded defaults in the ``config_defaults`` class variable (dict).
They are the lowest priority layer in the three-layer config resolution:

    config_defaults  <  AUTH_<NAME>__* env vars  <  DB per-org config

ENV variable naming convention:
    AUTH_<NAME_UPPERCASE>__<FIELD_UPPERCASE>
    e.g. AUTH_CORPSAML__IDP_URL  for a provider named "corpsaml"

Sub-directory layout example:
    custom_code/
        auth_backends/
            __init__.py          ← required
            corporate/
                __init__.py      ← required for walk_packages to recurse
                sso_saml.py
            example_backend.py   ← this file.

Authentication chain
--------------------
Each user-facing login request walks through the list of enabled providers
in the order defined on the organisation record (``auth_providers`` JSON list).

- If ``AuthResult.success = True``  → stop chain, user is authenticated.
- If ``AuthResult.error_type`` is   INVALID_CREDENTIALS / ACCOUNT_LOCKED /
  ACCOUNT_DISABLED / PASSWORD_EXPIRED  → stop chain (definitive rejection).
- Otherwise (USER_NOT_FOUND / PROVIDER_UNAVAILABLE / CONFIGURATION_ERROR)
  → try the next provider in the chain.

User provisioning
-----------------
For non-"local" providers, the login route calls ``upsert_external_user()``
from ``src.shared.auth.user_sync`` to auto-provision the user and sync their
role and ``GSageGroup`` memberships using the ``group_mapping`` config key.
"""

from __future__ import annotations

from typing import ClassVar

from src.shared.auth.base import (
    AuthErrorType,
    AuthIdentity,
    AuthResult,
    BaseAuthProvider,
)


class ExampleCustomBackend(BaseAuthProvider):
    """
    Minimal example auth backend — replace with your implementation.

    This dummy backend accepts any username/password where the password equals
    the username reversed.  Obviously not suitable for production use.
    """

    # Unique, lowercase, no spaces.  Used in AuthProvider names,
    # ENV variable prefixes and the org auth_providers JSON list.
    name: ClassVar[str] = "example_custom"

    # Human-readable label shown in the admin UI.
    display_name: ClassVar[str] = "Example Custom Backend"

    # Set to True once your implementation is ready.
    available: ClassVar[bool] = False

    # Key → any JSON-serialisable value.
    # These are the lowest-priority defaults overridden by env vars and DB config.
    config_defaults: ClassVar[dict] = {
        "some_setting": "default_value",
        "timeout_seconds": 10,
    }

    # Optional JSON Schema for the config dict (used for validation / UI hints).
    config_schema: ClassVar[dict | None] = {
        "properties": {
            "some_setting": {
                "type": "string",
                "description": "An example setting",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Request timeout in seconds",
            },
        },
        "required": [],
    }

    async def authenticate(
        self,
        username: str,
        password: str,
        config: dict,
    ) -> AuthResult:
        """Validate credentials and return an AuthResult.

        Parameters
        ----------
        username:
            The login identifier provided by the user.
        password:
            The plaintext password or token provided by the user.
        config:
            Merged 3-layer config dict (defaults + env + DB per-org).

        Returns
        -------
        AuthResult
            On success: ``success=True``, ``identity`` populated.
            On failure: ``success=False``, ``error_type`` set appropriately.
        """
        _ = config  # suppress unused-variable warning in the example

        # --- Replace this with real authentication logic ---
        if not self.available:
            # Backend disabled — skip this provider
            return AuthResult(
                success=False,
                error_type=AuthErrorType.CONFIGURATION_ERROR,
                error_message=f"{self.name} backend is disabled (available=False)",
            )

        if password != username[::-1]:
            return AuthResult(
                success=False,
                error_type=AuthErrorType.INVALID_CREDENTIALS,
                error_message="Invalid credentials",
            )

        return AuthResult(
            success=True,
            identity=AuthIdentity(
                email=f"{username}@example.com",
                full_name=username.title(),
            ),
            groups=[],  # List of group DNs/names from the external source
        )

    async def healthcheck(self, config: dict) -> bool:
        """Return True if the backend is reachable and correctly configured."""
        _ = config
        return self.available
