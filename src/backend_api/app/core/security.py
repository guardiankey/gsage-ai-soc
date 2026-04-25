"""gSage AI — security utilities for the backend API.

Re-exports the core auth functions from the shared layer and provides the
``build_token_claims`` helper that assembles a full JWT claims dict.
"""

from src.backend_api.app.core.tenant import TenantContext, permissions_for_role
from src.shared.security.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    verify_password,
)

__all__ = [
    # JWT helpers
    "build_token_claims",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    # API key helpers
    "generate_api_key",
    "hash_api_key",
    # Password helpers
    "hash_password",
    "verify_password",
    # Tenant
    "TenantContext",
    "permissions_for_role",
]


def build_token_claims(
    user_id: str,
    email: str,
    org_id: str,
    org_role: str,
) -> dict:
    """Build the full JWT claims dict for an access token.

    Args:
        user_id: String UUID of the user (becomes ``sub``).
        email: User's email address.
        org_id: String UUID of the organization.
        org_role: The user's role in the org (``owner``, ``admin``, ``member``, ``viewer``).

    Returns:
        Claims dict ready to pass to ``create_access_token``.
    """
    return {
        "sub": user_id,
        "email": email,
        "org_id": org_id,
        "org_role": org_role,
        "permissions": permissions_for_role(org_role),
    }

