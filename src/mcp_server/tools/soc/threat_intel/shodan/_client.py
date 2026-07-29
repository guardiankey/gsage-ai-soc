"""gSage AI — Shodan REST API async client.

Thin async wrapper over ``https://api.shodan.io`` built on :mod:`httpx`.
Authentication is a single API key sent as the ``key`` query parameter on
every request.

Usage
-----
::

    async with ShodanClient(api_key="...") as client:
        host = await client.get("/shodan/host/8.8.8.8")

The ``ShodanError`` exception is raised for all API-level and network
failures.  ``status_code`` carries the HTTP status (0 for transport errors);
``retryable`` is ``True`` for transient (5xx / timeout / connection) errors.
Shodan signals quota/plan problems with 401/403 and rate limits with 429.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

_BASE_URL = "https://api.shodan.io"
_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


class ShodanError(Exception):
    """Raised when the Shodan API returns an error or the connection fails."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ShodanClient:
    """Async Shodan REST API client.

    Parameters
    ----------
    api_key :
        Shodan API key.
    timeout :
        HTTP timeout in seconds (default: 20).
    base_url :
        Override the API base URL (default ``https://api.shodan.io``).
    """

    def __init__(
        self,
        api_key: str,
        timeout: int = 20,
        base_url: str = _BASE_URL,
    ) -> None:
        if not api_key:
            raise ShodanError("Shodan API key is required.", retryable=False)
        self._api_key = api_key
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")
        self._http: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "ShodanClient":
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
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
                log.debug("Shodan client close error (ignored): %s", exc)

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        """Issue a GET request and return the parsed JSON body.

        The API key is injected automatically as the ``key`` query param.
        """
        if self._http is None:
            raise ShodanError(
                "ShodanClient is not connected. Use as an async context manager.",
                retryable=False,
            )
        query = dict(params or {})
        query["key"] = self._api_key
        try:
            resp = await self._http.get(path, params=query)
        except httpx.TimeoutException as exc:
            raise ShodanError(f"Shodan request timed out ({path}): {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ShodanError(f"Shodan connection failed ({path}): {exc}", retryable=True) from exc

        return self._parse(resp, path)

    @staticmethod
    def _parse(resp: httpx.Response, path: str) -> Any:
        if resp.status_code == 401:
            raise ShodanError(
                "Shodan authentication failed: invalid API key.",
                status_code=401,
                retryable=False,
            )
        if resp.status_code == 403:
            raise ShodanError(
                "Shodan access denied: your plan does not permit this "
                "endpoint, or you are out of query credits.",
                status_code=403,
                retryable=False,
            )
        if resp.status_code == 404:
            raise ShodanError(
                f"Shodan: no information available ({path}).",
                status_code=404,
                retryable=False,
            )
        if resp.status_code >= 400:
            detail = resp.text[:300]
            try:
                body = resp.json()
                detail = body.get("error") or detail
            except ValueError:
                pass
            raise ShodanError(
                f"Shodan API error ({path}, HTTP {resp.status_code}): {detail}",
                status_code=resp.status_code,
                retryable=resp.status_code in _RETRYABLE_HTTP_CODES,
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ShodanError(
                f"Shodan returned non-JSON response ({path}).",
                status_code=resp.status_code,
                retryable=False,
            ) from exc
