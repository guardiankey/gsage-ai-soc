"""gSage AI — Censys Search API v2 async client.

Thin async wrapper over ``https://search.censys.io/api/v2`` built on
:mod:`httpx`.  Authentication is HTTP Basic with the account API ID (username)
and API Secret (password).

Usage
-----
::

    async with CensysClient(api_id="...", api_secret="...") as client:
        host = await client.get("/hosts/8.8.8.8")

The ``CensysError`` exception is raised for all API-level and network
failures.  ``status_code`` carries the HTTP status (0 for transport errors);
``retryable`` is ``True`` for transient (5xx / timeout / connection) errors.
Censys signals auth problems with 401/403 and rate limits with 429.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

_BASE_URL = "https://search.censys.io/api/v2"
_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


class CensysError(Exception):
    """Raised when the Censys API returns an error or the connection fails."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class CensysClient:
    """Async Censys Search API v2 client.

    Parameters
    ----------
    api_id :
        Censys API ID (HTTP Basic username).
    api_secret :
        Censys API Secret (HTTP Basic password).
    timeout :
        HTTP timeout in seconds (default: 20).
    base_url :
        Override the API base URL (default ``https://search.censys.io/api/v2``).
    """

    def __init__(
        self,
        api_id: str,
        api_secret: str,
        timeout: int = 20,
        base_url: str = _BASE_URL,
    ) -> None:
        if not api_id or not api_secret:
            raise CensysError(
                "Both 'api_id' and 'api_secret' are required.", retryable=False
            )
        self._api_id = api_id
        self._api_secret = api_secret
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")
        self._http: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "CensysClient":
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            auth=(self._api_id, self._api_secret),
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
        if self._http is not None:
            http = self._http
            self._http = None
            try:
                await http.aclose()
            except Exception as exc:  # noqa: BLE001
                log.debug("Censys client close error (ignored): %s", exc)

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        """Issue a GET request and return the parsed ``result`` object."""
        if self._http is None:
            raise CensysError(
                "CensysClient is not connected. Use as an async context manager.",
                retryable=False,
            )
        try:
            resp = await self._http.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise CensysError(f"Censys request timed out ({path}): {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise CensysError(f"Censys connection failed ({path}): {exc}", retryable=True) from exc

        return self._parse(resp, path)

    @staticmethod
    def _parse(resp: httpx.Response, path: str) -> Any:
        if resp.status_code in (401, 403):
            raise CensysError(
                "Censys authentication failed: invalid API ID or Secret.",
                status_code=resp.status_code,
                retryable=False,
            )
        if resp.status_code == 404:
            raise CensysError(
                f"Censys: resource not found ({path}).",
                status_code=404,
                retryable=False,
            )
        if resp.status_code >= 400:
            detail = resp.text[:300]
            try:
                body = resp.json()
                detail = body.get("error") or body.get("message") or detail
            except ValueError:
                pass
            raise CensysError(
                f"Censys API error ({path}, HTTP {resp.status_code}): {detail}",
                status_code=resp.status_code,
                retryable=resp.status_code in _RETRYABLE_HTTP_CODES,
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise CensysError(
                f"Censys returned non-JSON response ({path}).",
                status_code=resp.status_code,
                retryable=False,
            ) from exc

        # Censys envelope: {"code": 200, "status": "OK", "result": {...}}.
        if isinstance(body, dict) and "result" in body:
            return body["result"]
        return body
