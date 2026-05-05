"""gSage AI — EntraOIDCProvider (Microsoft Entra ID / Azure AD via OIDC).

Unlike LDAP/Local providers, OIDC is **not** a credentials-based protocol —
the user authenticates directly against Microsoft, and gSage receives an
``id_token`` after a redirect-based authorization code flow.

To plug into the existing :class:`BaseAuthProvider` chain (which is
credentials-driven), this provider:

- Implements ``authenticate()`` as a no-op that returns
  :class:`AuthErrorType.PROVIDER_UNAVAILABLE`, so the chain skips it
  gracefully when a password POST hits ``/v1/auth/login``.
- Exposes :meth:`get_authorize_url` and :meth:`exchange_code` which the
  dedicated SSO routes call directly.

After a successful ``exchange_code()``, the SSO route reuses
:func:`upsert_external_user` and the standard token issuance path, so the
identity provisioning flow is shared with LDAP/AD.

Configuration (per-org, stored encrypted in ``GSageOrganization.auth_config``)
----------------------------------------------------------------------------

- ``client_id``           — Application (client) ID from the Entra app reg.
- ``tenant_id``           — Directory (tenant) ID. Use ``common`` for
                            multi-tenant apps (not recommended for SSO).
- ``client_secret``       — Sensitive; stored encrypted.
- ``redirect_uri``        — Optional explicit redirect URI override. When
                            empty, defaults to
                            ``{public_base_url}/api/v1/auth/sso/{org_slug}/entra_oidc/callback``.
- ``scopes``              — Space-separated extra scopes (default
                            ``openid profile email User.Read``).
- ``default_role``        — Role assigned to users with no group_mapping match.
- ``group_mapping``       — ``{ "{group_object_id}": {"role": "...", "groups": [...]} }``.
- ``required_groups``     — Login gate (list of group object IDs).
- ``auto_create_groups``  — Auto-create local ``GSageGroup`` rows for
                            mapped names.
- ``auto_provision_users`` — When False, only previously-known users may
                             sign in (default True).
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, ClassVar, Optional
from urllib.parse import urlencode

import httpx

from src.shared.auth.base import (
    AuthErrorType,
    AuthIdentity,
    AuthResult,
    BaseAuthProvider,
)

logger = logging.getLogger(__name__)


def _discovery_url(tenant_id: str) -> str:
    return (
        f"https://login.microsoftonline.com/{tenant_id}/v2.0/"
        f".well-known/openid-configuration"
    )


class EntraOIDCProvider(BaseAuthProvider):
    """Microsoft Entra ID OpenID Connect (Authorization Code + PKCE)."""

    name = "entra_oidc"
    display_name = "Microsoft Entra ID"

    config_defaults: ClassVar[dict] = {
        "client_id": "",
        "tenant_id": "",
        "client_secret": "",
        "redirect_uri": "",
        "scopes": "openid profile email User.Read",
        "default_role": "viewer",
        "group_mapping": {},
        "required_groups": [],
        "auto_create_groups": True,
        "auto_create_departments": False,
        "auto_provision_users": True,
    }

    config_schema: ClassVar[dict] = {
        "properties": {
            "client_id": {
                "type": "string",
                "description": "Application (client) ID from the Entra app registration.",
            },
            "tenant_id": {
                "type": "string",
                "description": "Directory (tenant) ID. Use 'common' for multi-tenant.",
            },
            "client_secret": {
                "type": "string",
                "sensitive": True,
                "description": "Client secret value from the Entra app registration.",
            },
            "redirect_uri": {
                "type": "string",
                "description": (
                    "Optional explicit redirect URI override. Leave blank to "
                    "auto-derive from public_base_url and org slug."
                ),
            },
            "scopes": {
                "type": "string",
                "description": "Space-separated OIDC scopes.",
            },
            "default_role": {
                "type": "string",
                "description": "Role assigned to users matched by no group_mapping entry.",
            },
            "group_mapping": {
                "type": "object",
                "description": (
                    "Map external Entra group object IDs to local roles and groups."
                ),
            },
            "required_groups": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Login gate: list of Entra group object IDs the user must "
                    "belong to at least one of. Empty list (default) allows all."
                ),
            },
            "auto_create_groups": {
                "type": "boolean",
                "description": "Auto-create GSageGroup records for mapped groups.",
            },
            "auto_create_departments": {
                "type": "boolean",
                "description": (
                    "Auto-create GSageDepartment records when a group_mapping "
                    "entry references a department that does not yet exist."
                ),
            },
            "auto_provision_users": {
                "type": "boolean",
                "description": (
                    "When False, refuse logins from users who do not already "
                    "exist locally."
                ),
            },
        },
        "required": ["client_id", "tenant_id", "client_secret"],
    }

    # ──────────────────────────────────────────────────────────────────────
    # BaseAuthProvider — credentials path is not used by this provider
    # ──────────────────────────────────────────────────────────────────────

    async def authenticate(
        self,
        username: str,
        password: str,
        config: dict,
    ) -> AuthResult:
        """OIDC is not a credential-bearer protocol.

        The credential-based chain runner skips this provider via
        :class:`AuthErrorType.PROVIDER_UNAVAILABLE`.
        """
        return AuthResult(
            success=False,
            error_type=AuthErrorType.PROVIDER_UNAVAILABLE,
            error_message=(
                "Entra OIDC requires the dedicated /v1/auth/sso/* flow; "
                "skipping credentials chain."
            ),
        )

    # ──────────────────────────────────────────────────────────────────────
    # OIDC discovery
    # ──────────────────────────────────────────────────────────────────────

    async def _discover(self, tenant_id: str) -> dict[str, Any]:
        url = _discovery_url(tenant_id)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    # ──────────────────────────────────────────────────────────────────────
    # Authorization URL
    # ──────────────────────────────────────────────────────────────────────

    async def build_authorize_url(
        self,
        config: dict,
        *,
        state: str,
        code_challenge: str,
        nonce: str,
        redirect_uri: str,
    ) -> str:
        """Build the Entra ``authorize`` URL for the redirect to Microsoft."""
        tenant_id = config.get("tenant_id") or ""
        client_id = config.get("client_id") or ""
        scopes = config.get("scopes") or "openid profile email User.Read"

        if not tenant_id or not client_id:
            raise ValueError("entra_oidc: client_id and tenant_id are required")

        discovery = await self._discover(tenant_id)
        authorize_endpoint = discovery["authorization_endpoint"]

        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "response_mode": "query",
        }
        return f"{authorize_endpoint}?{urlencode(params)}"

    # ──────────────────────────────────────────────────────────────────────
    # Authorization code exchange + ID token validation
    # ──────────────────────────────────────────────────────────────────────

    async def exchange_code(
        self,
        config: dict,
        *,
        code: str,
        code_verifier: str,
        nonce: str,
        redirect_uri: str,
    ) -> AuthResult:
        """Exchange the authorization code, validate the id_token, and
        return a populated :class:`AuthResult`.

        The caller (SSO route) is responsible for invoking
        :func:`upsert_external_user` and issuing application JWTs.
        """
        tenant_id = config.get("tenant_id") or ""
        client_id = config.get("client_id") or ""
        client_secret = config.get("client_secret") or ""
        if not (tenant_id and client_id and client_secret):
            return AuthResult(
                success=False,
                error_type=AuthErrorType.CONFIGURATION_ERROR,
                error_message="entra_oidc: client_id/tenant_id/client_secret missing",
            )

        try:
            discovery = await self._discover(tenant_id)
        except Exception as exc:
            logger.error("entra_oidc: discovery failed — %s", exc)
            return AuthResult(
                success=False,
                error_type=AuthErrorType.PROVIDER_UNAVAILABLE,
                error_message=f"OIDC discovery failed: {exc}",
            )

        token_endpoint = discovery["token_endpoint"]
        jwks_uri = discovery["jwks_uri"]
        issuer = discovery["issuer"]

        # 1. Exchange code → tokens
        async with httpx.AsyncClient(timeout=15) as http:
            try:
                tok_resp = await http.post(
                    token_endpoint,
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "code_verifier": code_verifier,
                    },
                )
            except Exception as exc:
                logger.error("entra_oidc: token request failed — %s", exc)
                return AuthResult(
                    success=False,
                    error_type=AuthErrorType.PROVIDER_UNAVAILABLE,
                    error_message=f"Token endpoint error: {exc}",
                )

            if tok_resp.status_code != 200:
                logger.warning(
                    "entra_oidc: token exchange returned %s — %s",
                    tok_resp.status_code, tok_resp.text,
                )
                return AuthResult(
                    success=False,
                    error_type=AuthErrorType.INVALID_CREDENTIALS,
                    error_message="Authorization code exchange rejected by Entra",
                )

            tokens = tok_resp.json()

            id_token = tokens.get("id_token")
            access_token = tokens.get("access_token")
            if not id_token:
                return AuthResult(
                    success=False,
                    error_type=AuthErrorType.PROVIDER_UNAVAILABLE,
                    error_message="Token endpoint returned no id_token",
                )

            # 2. Validate id_token (signature, iss, aud, exp, nonce)
            try:
                claims = await self._validate_id_token(
                    http, id_token, jwks_uri, issuer, client_id, nonce,
                )
            except _IDTokenError as exc:
                logger.warning("entra_oidc: id_token validation failed — %s", exc)
                return AuthResult(
                    success=False,
                    error_type=AuthErrorType.INVALID_CREDENTIALS,
                    error_message=str(exc),
                )

            # 3. Build identity
            user_oid = str(claims.get("oid") or claims.get("sub") or "")
            email = (
                claims.get("email")
                or claims.get("preferred_username")
                or claims.get("upn")
                or ""
            )
            full_name = (
                claims.get("name")
                or " ".join(
                    filter(
                        None,
                        [claims.get("given_name"), claims.get("family_name")],
                    )
                )
                or email
            )
            if not email or not user_oid:
                return AuthResult(
                    success=False,
                    error_type=AuthErrorType.INVALID_CREDENTIALS,
                    error_message="id_token missing email or oid",
                )

            # 4. Resolve groups (with Microsoft Graph fallback for overage)
            groups = await self._resolve_groups(
                http=http,
                claims=claims,
                access_token=access_token,
                user_oid=user_oid,
            )

            # 5. Required-groups gate
            required_groups: list[str] = list(config.get("required_groups") or [])
            if required_groups:
                allowed = {g.lower() for g in required_groups}
                user_groups = {g.lower() for g in groups}
                if not (allowed & user_groups):
                    return AuthResult(
                        success=False,
                        error_type=AuthErrorType.ACCOUNT_DISABLED,
                        error_message=(
                            "User is not a member of any required Entra group."
                        ),
                    )

        return AuthResult(
            success=True,
            identity=AuthIdentity(
                email=email.lower(),
                full_name=full_name,
                external_id=user_oid,
            ),
            groups=groups,
            provider_name=self.name,
            extra_claims={
                "iss": claims.get("iss"),
                "tid": claims.get("tid"),
            },
        )

    # ──────────────────────────────────────────────────────────────────────
    # ID token validation (authlib JOSE if available, fallback to PyJWT)
    # ──────────────────────────────────────────────────────────────────────

    async def _validate_id_token(
        self,
        http: httpx.AsyncClient,
        id_token: str,
        jwks_uri: str,
        issuer: str,
        client_id: str,
        nonce: str,
    ) -> dict[str, Any]:
        # Fetch JWKS
        try:
            jwks_resp = await http.get(jwks_uri)
            jwks_resp.raise_for_status()
            jwks = jwks_resp.json()
        except Exception as exc:
            raise _IDTokenError(f"Could not fetch JWKS: {exc}") from exc

        # Prefer authlib (richer JOSE support) when available; fall back to PyJWT.
        try:
            from authlib.jose import JsonWebToken  # type: ignore[import-not-found]

            claims = JsonWebToken(["RS256"]).decode(id_token, jwks)
            claims.validate()
            data: dict[str, Any] = dict(claims)
        except ImportError:
            import jwt  # type: ignore[import-not-found]
            from jwt import PyJWKClient  # type: ignore[import-not-found]

            unverified = jwt.get_unverified_header(id_token)
            kid = unverified.get("kid")
            signing_key = None
            for key in jwks.get("keys", []):
                if key.get("kid") == kid:
                    signing_key = jwt.algorithms.RSAAlgorithm.from_jwk(key)  # type: ignore[attr-defined]
                    break
            if signing_key is None:
                raise _IDTokenError(f"No JWKS key found for kid={kid!r}")
            data = jwt.decode(
                id_token,
                signing_key,
                algorithms=["RS256"],
                audience=client_id,
                options={"verify_aud": True, "verify_exp": True},
            )
        except Exception as exc:
            raise _IDTokenError(f"id_token decode failed: {exc}") from exc

        # Issuer check (Entra rewrites issuer with concrete tenant id even when
        # configured with 'common', so accept any matching prefix).
        token_iss = str(data.get("iss") or "")
        if not token_iss.startswith("https://login.microsoftonline.com/"):
            # Older v1 endpoint also possible
            if not token_iss.startswith("https://sts.windows.net/"):
                raise _IDTokenError(f"Unexpected issuer: {token_iss!r}")
        # If discovery returned a concrete issuer (single-tenant), enforce equality
        if "{tenantid}" not in issuer and token_iss != issuer:
            raise _IDTokenError(
                f"Issuer mismatch: token={token_iss!r} expected={issuer!r}"
            )

        # Audience check (already enforced by PyJWT path; recheck for authlib)
        aud = data.get("aud")
        if isinstance(aud, list):
            if client_id not in aud:
                raise _IDTokenError("client_id not in id_token aud[]")
        elif aud != client_id:
            raise _IDTokenError(f"Audience mismatch: aud={aud!r}")

        # Nonce check (defends against replay)
        token_nonce = data.get("nonce")
        if token_nonce != nonce:
            raise _IDTokenError("nonce mismatch")

        return data

    # ──────────────────────────────────────────────────────────────────────
    # Group resolution (with Graph fallback for >200 group overage)
    # ──────────────────────────────────────────────────────────────────────

    async def _resolve_groups(
        self,
        http: httpx.AsyncClient,
        claims: dict[str, Any],
        access_token: Optional[str],
        user_oid: str,
    ) -> list[str]:
        # Direct claim
        groups = claims.get("groups")
        if isinstance(groups, list) and groups:
            return [str(g) for g in groups]

        # Overage indicator: _claim_names.groups points to a Graph endpoint
        claim_names = claims.get("_claim_names") or {}
        if "groups" not in claim_names:
            return []

        if not access_token:
            logger.warning(
                "entra_oidc: groups overage signalled but no access_token to call Graph"
            )
            return []

        try:
            resp = await http.post(
                "https://graph.microsoft.com/v1.0/me/getMemberObjects",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"securityEnabledOnly": False},
            )
            if resp.status_code != 200:
                logger.warning(
                    "entra_oidc: Graph getMemberObjects returned %s — %s",
                    resp.status_code, resp.text,
                )
                return []
            data = resp.json() or {}
            value = data.get("value") or []
            return [str(g) for g in value]
        except Exception as exc:
            logger.warning("entra_oidc: Graph fallback failed — %s", exc)
            return []

    # ──────────────────────────────────────────────────────────────────────
    # Helpers used by the SSO route
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def generate_state() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def generate_nonce() -> str:
        return secrets.token_urlsafe(16)

    @staticmethod
    def generate_pkce_pair() -> tuple[str, str]:
        """Return (code_verifier, code_challenge) for PKCE S256."""
        import base64
        import hashlib

        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        return verifier, challenge


class _IDTokenError(Exception):
    """Raised internally when id_token validation fails."""
