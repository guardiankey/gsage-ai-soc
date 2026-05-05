"""gSage AI — OAuth2 token acquisition for email accounts (XOAUTH2).

Implements the **client-credentials** flow against Microsoft Identity
Platform (Azure AD) so the IMAP/SMTP workers can authenticate to
Exchange Online (Office 365) without basic auth — which Microsoft
disabled for IMAP/SMTP in 2022.

Flow
----
1. POST to ``https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token``
   with ``grant_type=client_credentials``,
   ``client_id``, ``client_secret``, ``scope``.
2. Response yields ``access_token`` + ``expires_in`` (seconds).
3. Token is cached in Redis (``oauth:email:{account_id}``) with TTL
   = ``expires_in - 60s`` so concurrent workers reuse it.

Pre-requisites (one-time, performed by tenant admin)
----------------------------------------------------
* Register an app in Azure AD.
* Grant ``IMAP.AccessAsApp`` and ``SMTP.SendAsApp`` *application*
  permissions (admin consent required).
* Authorise the app to access each mailbox via Exchange Online
  PowerShell (``New-ServicePrincipal`` + ``Add-MailboxPermission``).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Optional

import base64
import httpx
import redis.asyncio as redis

from src.shared.config.settings import get_settings

if TYPE_CHECKING:
    from src.shared.models.email_account import GSageEmailAccount

logger = logging.getLogger(__name__)

DEFAULT_SCOPE = "https://outlook.office365.com/.default"
DEFAULT_TOKEN_URL_TEMPLATE = (
    "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
)
TOKEN_CACHE_KEY = "oauth:email:{account_id}"
# Refresh tokens at least 60 seconds before they expire so in-flight
# requests do not race with the rotation.
TOKEN_REFRESH_LEEWAY = 60


def _mask(value: Optional[str], keep: int = 4) -> str:
    """Mask a credential string for logging, keeping the last *keep* chars."""
    if not value:
        return "<empty>"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{'*' * (len(value) - keep)}{value[-keep:]}"


def _decode_jwt_payload_unverified(token: str) -> dict[str, Any]:
    """Decode the payload of a JWT without verifying the signature.

    Used **only for diagnostics**; never use the returned claims to make
    security decisions. Returns ``{}`` if the token is not a parseable JWT.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        # base64url with missing padding
        padding = "=" * (-len(payload_b64) % 4)
        decoded = base64.urlsafe_b64decode(payload_b64 + padding)
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _summarise_token_for_log(token: str) -> dict[str, Any]:
    """Extract diagnostic-friendly claims from an access token."""
    claims = _decode_jwt_payload_unverified(token)
    if not claims:
        return {"jwt_decoded": False}
    return {
        "jwt_decoded": True,
        "aud": claims.get("aud"),
        "iss": claims.get("iss"),
        "appid": claims.get("appid") or claims.get("azp"),
        "tid": claims.get("tid"),
        "roles": claims.get("roles"),
        "scp": claims.get("scp"),
        "idtyp": claims.get("idtyp"),
        "app_displayname": claims.get("app_displayname"),
        "oid": claims.get("oid"),
    }


class OAuthTokenError(Exception):
    """Raised when an OAuth2 token cannot be obtained."""


async def get_access_token(
    account: "GSageEmailAccount",
    *,
    redis_client: Optional[redis.Redis] = None,
    force_refresh: bool = False,
) -> str:
    """Return a valid OAuth2 access token for the given email account.

    Parameters
    ----------
    account:
        The email account row.  Must have ``auth_method='oauth2'`` and
        ``oauth_tenant_id`` / ``oauth_client_id`` /
        ``oauth_client_secret`` populated.
    redis_client:
        Optional pre-built async Redis client.  When omitted a short-
        lived client is built from ``settings.redis_url`` and closed
        before the function returns.
    force_refresh:
        Skip the cache and request a new token.

    Raises
    ------
    OAuthTokenError
        When the account is mis-configured or the token endpoint
        rejects the request.
    """
    if account.auth_method != "oauth2":
        raise OAuthTokenError(
            f"Account {account.email} is not configured for OAuth2 "
            f"(auth_method={account.auth_method!r})."
        )

    tenant_id = (account.oauth_tenant_id or "").strip()
    client_id = (account.oauth_client_id or "").strip()
    client_secret = account.oauth_client_secret  # decrypted
    if not tenant_id or not client_id or not client_secret:
        raise OAuthTokenError(
            f"Account {account.email} is missing OAuth2 credentials "
            "(tenant_id / client_id / client_secret)."
        )

    cache_key = TOKEN_CACHE_KEY.format(account_id=account.id)
    owns_redis = redis_client is None
    if redis_client is None:
        settings = get_settings()
        redis_client = redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )

    try:
        if not force_refresh:
            cached = await redis_client.get(cache_key)
            if cached:
                try:
                    payload = json.loads(cached)
                    token = payload.get("access_token")
                    if token:
                        return token
                except (json.JSONDecodeError, TypeError):
                    pass  # fall through and refetch

        token_url = (account.oauth_token_endpoint or "").strip() or (
            DEFAULT_TOKEN_URL_TEMPLATE.format(tenant_id=tenant_id)
        )
        scope = (account.oauth_scope or "").strip() or DEFAULT_SCOPE

        body = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
        }
        logger.info(
            "OAuth2 token: requesting access_token account=%s tenant=%s "
            "client_id=%s scope=%s endpoint=%s",
            account.email,
            tenant_id,
            _mask(client_id),
            scope,
            token_url,
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as http:
                resp = await http.post(token_url, data=body)
        except httpx.HTTPError as exc:
            raise OAuthTokenError(
                f"OAuth2 token request failed for {account.email}: {exc}"
            ) from exc

        if resp.status_code >= 400:
            # AAD returns JSON with `error`, `error_description`,
            # `error_codes`, `correlation_id`, `trace_id`. Surface them
            # explicitly to help diagnose mis-configuration.
            err_payload: dict = {}
            try:
                err_payload = resp.json() or {}
            except ValueError:
                err_payload = {}
            logger.error(
                "OAuth2 token endpoint rejected request account=%s status=%d "
                "error=%s error_description=%r error_codes=%s correlation_id=%s "
                "trace_id=%s endpoint=%s tenant=%s scope=%s client_id=%s",
                account.email,
                resp.status_code,
                err_payload.get("error"),
                err_payload.get("error_description"),
                err_payload.get("error_codes"),
                err_payload.get("correlation_id"),
                err_payload.get("trace_id"),
                token_url,
                tenant_id,
                scope,
                _mask(client_id),
            )
            raise OAuthTokenError(
                f"OAuth2 token endpoint returned HTTP {resp.status_code} "
                f"for {account.email}: {resp.text[:500]}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise OAuthTokenError(
                f"OAuth2 token response is not JSON for {account.email}: "
                f"{resp.text[:300]}"
            ) from exc

        access_token = data.get("access_token")
        expires_in = int(data.get("expires_in") or 0)
        if not access_token or expires_in <= 0:
            raise OAuthTokenError(
                f"OAuth2 token response missing access_token/expires_in "
                f"for {account.email}: {data}"
            )

        ttl = max(expires_in - TOKEN_REFRESH_LEEWAY, 60)
        await redis_client.setex(
            cache_key, ttl, json.dumps({"access_token": access_token})
        )
        token_info = _summarise_token_for_log(access_token)
        logger.info(
            "OAuth2 token: cached new access_token account=%s ttl=%ds "
            "expires_in=%ds token_type=%s returned_scope=%s token_len=%d "
            "claims=%s",
            account.email,
            ttl,
            expires_in,
            data.get("token_type"),
            data.get("scope"),
            len(access_token),
            token_info,
        )

        # Diagnostic guard: if this is an Exchange Online IMAP token but the
        # required app-role is missing, surface a clear warning. Without
        # `IMAP.AccessAsApp` in `roles`, EXO will reject XOAUTH2 with the
        # opaque "AUTHENTICATE failed." message no matter what.
        if token_info.get("jwt_decoded"):
            roles = token_info.get("roles") or []
            aud = token_info.get("aud") or ""
            if "outlook.office" in str(aud) or "outlook.office365.com" in scope:
                if "IMAP.AccessAsApp" not in roles:
                    logger.warning(
                        "OAuth2 token: missing 'IMAP.AccessAsApp' app role for "
                        "account=%s. Token roles=%s. Add the application "
                        "permission 'Office 365 Exchange Online → "
                        "IMAP.AccessAsApp' to the app registration and grant "
                        "admin consent.",
                        account.email,
                        roles,
                    )
                else:
                    logger.info(
                        "OAuth2 token: IMAP.AccessAsApp role present — if "
                        "XOAUTH2 still fails, the service principal likely "
                        "lacks FullAccess on the mailbox (run "
                        "Add-MailboxPermission via Exchange Online "
                        "PowerShell). account=%s",
                        account.email,
                    )
        return access_token
    finally:
        if owns_redis:
            await redis_client.aclose()


def build_xoauth2_string(username: str, access_token: str) -> str:
    """Build the SASL XOAUTH2 SASL string used by both IMAP and SMTP."""
    return f"user={username}\x01auth=Bearer {access_token}\x01\x01"
