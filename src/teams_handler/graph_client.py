"""gSage AI — Microsoft Graph client (Teams channel).

Used **only** for first-contact resolution: when a Teams activity
arrives from a sender whose ``aadObjectId`` is not yet stored on any
``GSageUser.teams_aad_object_id``, we look up the user's primary
e-mail in Microsoft Graph and match it against ``GSageUser.email``.

Subsequent messages from the same sender resolve directly from the
database — Graph is never called again for that user.

Authentication uses the OAuth2 client-credentials flow with the bot's
own App Registration. The same ``app_id`` / ``app_password`` /
``tenant_id`` configured on the org's Teams ``InterfaceProfile``
is reused, so no extra Azure setup is required (the App Registration
must be granted ``User.Read.All`` *application* permission in the
target tenant).

Tokens and email lookups are cached in Redis to keep latency low and
to stay well under Graph's per-app rate limits.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_TOKEN_ENDPOINT = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_GRAPH_USER_ENDPOINT = "https://graph.microsoft.com/v1.0/users/{aad_id}"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"

_TOKEN_KEY = "teams:graph_token:{app_id}"
_EMAIL_KEY = "teams:user_email:{aad_id}"

_TOKEN_SAFETY_MARGIN = 60  # refresh 60 s before expiry


class GraphClient:
    """Lightweight per-profile Microsoft Graph client.

    Construct once per Teams ``InterfaceProfile``; safe to reuse across
    requests. All cache I/O goes through the optional Redis client; if
    no Redis is provided, an in-process token cache is used (fine for
    single-instance dev setups).
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_password: str,
        tenant_id: str,
        redis_client: Optional[Any] = None,
        http_timeout: float = 10.0,
        email_cache_ttl: int = 86_400,
    ) -> None:
        if not (app_id and app_password and tenant_id):
            raise ValueError(
                "GraphClient requires app_id, app_password and tenant_id"
            )
        self._app_id = app_id
        self._app_password = app_password
        self._tenant_id = tenant_id
        self._redis = redis_client
        self._timeout = http_timeout
        self._email_cache_ttl = email_cache_ttl

        # In-process fallback cache (used when Redis is unavailable).
        self._cached_token: Optional[str] = None
        self._cached_token_exp: float = 0.0

    # ── Public API ────────────────────────────────────────────────────

    async def lookup_email(self, aad_object_id: str) -> Optional[str]:
        """Resolve an Azure AD Object ID to the user's primary e-mail.

        Returns ``None`` if Graph could not resolve the user (e.g. user
        deleted, insufficient permissions, transient error).
        """
        if not aad_object_id:
            return None

        # Redis cache hit?
        if self._redis is not None:
            try:
                cached = await self._redis.get(
                    _EMAIL_KEY.format(aad_id=aad_object_id)
                )
                if cached:
                    return cached.decode() if isinstance(cached, bytes) else cached
            except Exception:  # pragma: no cover — best-effort cache
                logger.debug("graph email cache read failed", exc_info=True)

        token = await self._get_access_token()
        if token is None:
            return None

        url = _GRAPH_USER_ENDPOINT.format(aad_id=aad_object_id)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    params={"$select": "id,mail,userPrincipalName"},
                )
        except httpx.RequestError as exc:
            logger.warning(
                "graph lookup network error — aad_id=%s err=%s",
                aad_object_id,
                exc,
            )
            return None

        if resp.status_code == 404:
            logger.info("graph lookup: user not found — aad_id=%s", aad_object_id)
            return None
        if not resp.is_success:
            logger.warning(
                "graph lookup HTTP %s — aad_id=%s body=%s",
                resp.status_code,
                aad_object_id,
                resp.text[:200],
            )
            return None

        body = resp.json() if resp.content else {}
        # Prefer ``mail`` (primary SMTP), fall back to UPN (often = email).
        email = body.get("mail") or body.get("userPrincipalName")
        if not email:
            return None

        if self._redis is not None:
            try:
                await self._redis.set(
                    _EMAIL_KEY.format(aad_id=aad_object_id),
                    email,
                    ex=self._email_cache_ttl,
                )
            except Exception:  # pragma: no cover
                logger.debug("graph email cache write failed", exc_info=True)

        return email

    # ── Token management ──────────────────────────────────────────────

    async def _get_access_token(self) -> Optional[str]:
        """Return a cached or freshly minted Graph access token."""
        now = time.time()

        # In-process cache (still valid?)
        if self._cached_token and self._cached_token_exp - _TOKEN_SAFETY_MARGIN > now:
            return self._cached_token

        # Redis cache
        if self._redis is not None:
            try:
                cached = await self._redis.get(_TOKEN_KEY.format(app_id=self._app_id))
                if cached:
                    data = json.loads(
                        cached.decode() if isinstance(cached, bytes) else cached
                    )
                    if data.get("exp", 0) - _TOKEN_SAFETY_MARGIN > now:
                        self._cached_token = data["token"]
                        self._cached_token_exp = data["exp"]
                        return self._cached_token
            except Exception:  # pragma: no cover
                logger.debug("graph token cache read failed", exc_info=True)

        token, expires_in = await self._mint_token()
        if token is None:
            return None

        self._cached_token = token
        self._cached_token_exp = now + expires_in

        if self._redis is not None:
            try:
                await self._redis.set(
                    _TOKEN_KEY.format(app_id=self._app_id),
                    json.dumps({"token": token, "exp": self._cached_token_exp}),
                    ex=max(int(expires_in) - _TOKEN_SAFETY_MARGIN, 60),
                )
            except Exception:  # pragma: no cover
                logger.debug("graph token cache write failed", exc_info=True)

        return token

    async def _mint_token(self) -> tuple[Optional[str], int]:
        """Hit the AAD token endpoint and return (token, expires_in)."""
        url = _TOKEN_ENDPOINT.format(tenant=self._tenant_id)
        data = {
            "grant_type": "client_credentials",
            "client_id": self._app_id,
            "client_secret": self._app_password,
            "scope": _GRAPH_SCOPE,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, data=data)
        except httpx.RequestError as exc:
            logger.warning("graph token network error: %s", exc)
            return None, 0

        if not resp.is_success:
            logger.warning(
                "graph token mint HTTP %s body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return None, 0

        payload = resp.json()
        return payload.get("access_token"), int(payload.get("expires_in", 0))
