"""gSage AI — Wazuh Manager REST API async client.

Thin async wrapper over the Wazuh Manager API (default ``https://<host>:55000``)
built on :mod:`httpx`.  The Wazuh API is natively HTTP/JSON, so — unlike the
Zabbix/MISP clients — no ``asyncio.to_thread`` bridging is required.

Authentication
--------------
Wazuh issues a short-lived JWT.  The flow is:

1. ``POST /security/user/authenticate`` with HTTP Basic (``user``/``password``)
   → ``{"data": {"token": "<jwt>"}}``.
2. Every subsequent request carries ``Authorization: Bearer <jwt>``.

The token is cached on the client instance and transparently re-fetched on a
``401`` (expired token) exactly once per call.

Usage
-----
::

    async with WazuhClient(
        url="https://wazuh.example.com:55000",
        username="wazuh-wui",
        password="secret",
    ) as client:
        agents = await client.request("GET", "/agents", params={"limit": 50})

The ``WazuhError`` exception is raised for all API-level, auth or network
failures.  ``status_code`` carries the HTTP status (0 for transport errors);
``retryable`` is ``True`` for transient (5xx / timeout / connection) errors.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


class WazuhError(Exception):
    """Raised when the Wazuh API returns an error or the connection fails.

    Attributes
    ----------
    status_code : int
        HTTP status code (0 for connection/parse/auth transport errors).
    retryable : bool
        Whether the caller can safely retry this error.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class WazuhClient:
    """Async Wazuh Manager API client.

    Parameters
    ----------
    url :
        Base URL of the Wazuh Manager API, including the port
        (e.g. ``https://wazuh.example.com:55000``).
    username :
        Wazuh API user (e.g. ``wazuh`` or ``wazuh-wui``).
    password :
        Wazuh API password.
    verify_tls :
        Whether to verify the server TLS certificate (default: ``True``).
        Wazuh ships with a self-signed certificate by default, so many
        deployments set this to ``False``.
    timeout :
        HTTP timeout in seconds (default: 30).
    """

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        verify_tls: bool = True,
        timeout: int = 30,
    ) -> None:
        if not url:
            raise WazuhError("Wazuh API URL is required.", retryable=False)
        if not username or not password:
            raise WazuhError(
                "Both 'username' and 'password' must be provided in the "
                "Wazuh tool configuration.",
                retryable=False,
            )
        self._url = url.rstrip("/")
        self._username = username
        self._password = password
        self._verify_tls = verify_tls
        self._timeout = timeout
        self._token: Optional[str] = None
        self._http: Optional[httpx.AsyncClient] = None

    # ── Context manager ────────────────────────────────────────────────────

    async def __aenter__(self) -> "WazuhClient":
        self._http = httpx.AsyncClient(
            base_url=self._url,
            verify=self._verify_tls,
            timeout=self._timeout,
        )
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._http is not None:
            http = self._http
            self._http = None
            try:
                await http.aclose()
            except Exception as exc:  # noqa: BLE001
                log.debug("Wazuh client close error (ignored): %s", exc)

    # ── Authentication ─────────────────────────────────────────────────────

    async def _authenticate(self) -> str:
        """Fetch a fresh JWT from the Wazuh API and cache it.

        Only ever called from :meth:`request`, which guarantees ``self._http``
        is connected before dispatching.
        """
        try:
            resp = await self._http.post(
                "/security/user/authenticate",
                auth=(self._username, self._password),
            )
        except httpx.TimeoutException as exc:
            raise WazuhError(
                f"Wazuh authentication timed out: {exc}", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise WazuhError(
                f"Wazuh authentication connection failed: {exc}", retryable=True
            ) from exc

        if resp.status_code in (401, 403):
            raise WazuhError(
                "Wazuh authentication failed: invalid username or password.",
                status_code=resp.status_code,
                retryable=False,
            )
        if resp.status_code >= 400:
            raise WazuhError(
                f"Wazuh authentication error (HTTP {resp.status_code}): {resp.text[:300]}",
                status_code=resp.status_code,
                retryable=resp.status_code in _RETRYABLE_HTTP_CODES,
            )

        try:
            token = resp.json()["data"]["token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise WazuhError(
                "Wazuh authentication response did not contain a token.",
                status_code=resp.status_code,
                retryable=False,
            ) from exc

        self._token = token
        log.debug("Wazuh authenticated: url=%s user=%s", self._url, self._username)
        return token

    # ── Public request interface ───────────────────────────────────────────

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Any:
        """Invoke a Wazuh API endpoint, returning the parsed ``data`` object.

        Handles JWT acquisition and one transparent re-authentication when the
        token has expired (HTTP 401).

        Parameters
        ----------
        method :
            HTTP verb (``GET``, ``POST``, ``PUT``, ``DELETE``).
        path :
            API path starting with ``/`` (e.g. ``/agents``).
        params :
            Query-string parameters.
        json_body :
            JSON request body (for ``POST``/``PUT``).

        Returns
        -------
        Any
            The ``data`` field of the Wazuh API envelope
            (``{"data": ..., "error": 0, "message": ...}``).

        Raises
        ------
        WazuhError
            On API errors, authentication failures, or network issues.
        """
        if self._http is None:
            raise WazuhError(
                "WazuhClient is not connected. Use as an async context manager.",
                retryable=False,
            )

        if self._token is None:
            await self._authenticate()

        resp = await self._send(method, path, params, json_body)

        # Token expired mid-session — re-authenticate once and retry.
        if resp.status_code == 401:
            log.debug("Wazuh token expired, re-authenticating (path=%s)", path)
            self._token = None
            await self._authenticate()
            resp = await self._send(method, path, params, json_body)

        return self._parse(resp, method, path)

    async def _send(
        self,
        method: str,
        path: str,
        params: Optional[dict],
        json_body: Optional[dict],
    ) -> httpx.Response:
        # self._http is guaranteed non-None: request() guards it before
        # dispatching to this private helper.
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            return await self._http.request(
                method.upper(),
                path,
                params=params,
                json=json_body,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise WazuhError(
                f"Wazuh API request timed out ({method} {path}): {exc}",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise WazuhError(
                f"Wazuh API connection failed ({method} {path}): {exc}",
                retryable=True,
            ) from exc

    @staticmethod
    def _parse(resp: httpx.Response, method: str, path: str) -> Any:
        """Validate the HTTP status and unwrap the Wazuh JSON envelope."""
        if resp.status_code == 401:
            raise WazuhError(
                "Wazuh authorization failed after re-authentication.",
                status_code=401,
                retryable=False,
            )
        if resp.status_code == 403:
            raise WazuhError(
                f"Wazuh API forbidden ({method} {path}): the configured user "
                "lacks RBAC permission for this action.",
                status_code=403,
                retryable=False,
            )
        if resp.status_code >= 400:
            # Wazuh returns a structured error body; surface its message.
            detail = resp.text[:300]
            try:
                body = resp.json()
                detail = body.get("detail") or body.get("title") or detail
            except ValueError:
                pass
            raise WazuhError(
                f"Wazuh API error ({method} {path}, HTTP {resp.status_code}): {detail}",
                status_code=resp.status_code,
                retryable=resp.status_code in _RETRYABLE_HTTP_CODES,
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise WazuhError(
                f"Wazuh API returned non-JSON response ({method} {path}).",
                status_code=resp.status_code,
                retryable=False,
            ) from exc

        # Wazuh envelope: {"data": {...}, "error": 0, "message": "..."}.
        # A non-zero "error" indicates a logical failure even on HTTP 200.
        if isinstance(body, dict) and body.get("error"):
            raise WazuhError(
                f"Wazuh API logical error ({method} {path}): "
                f"{body.get('message', 'unknown error')} (error={body['error']})",
                status_code=resp.status_code,
                retryable=False,
            )

        return body.get("data", body) if isinstance(body, dict) else body
