"""gSage AI — BitDefender GravityZone JSON-RPC client.

Provides a thin async wrapper over the GravityZone Public API.
Handles Basic Auth and the JSON-RPC 2.0 envelope transparently,
including pagination helpers.

Authentication
--------------
The GravityZone API uses HTTP Basic Auth where the username is the API
key and the password is empty::

    Authorization: Basic base64(api_key:)

API keys are generated in the GravityZone console, under
*My Account → Control Center API keys*.

Usage
-----
::

    async with GravityZoneClient(api_key="...", base_url="...") as client:
        result = await client.call("network", "getEndpointsList", {
            "page": 1, "perPage": 30
        })

    # Paginated convenience:
    async with GravityZoneClient(api_key="...", base_url="...") as client:
        items = await client.call_paginated(
            "network", "getEndpointsList", {"perPage": 100}, max_pages=10
        )
"""

from __future__ import annotations

import base64
import logging
import uuid
from types import TracebackType
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://cloud.gravityzone.bitdefender.com/api"
_DEFAULT_TIMEOUT = 30.0


class GravityZoneError(Exception):
    """Raised when the GravityZone API returns an error response.

    Attributes
    ----------
    status_code : int
        HTTP status code (0 for connection/parse errors).
    code : int
        JSON-RPC error code returned by the API.
    message : str
        Human-readable error message.
    """

    def __init__(self, message: str, status_code: int = 0, code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class GravityZoneClient:
    """Async BitDefender GravityZone JSON-RPC API client.

    Parameters
    ----------
    api_key :
        GravityZone API key.  Configure via ``TOOL_{TOOL_NAME}__API_KEY``
        env var or the tool's DB config row.
    base_url :
        GravityZone API base URL.  Configure via ``TOOL_{TOOL_NAME}__BASE_URL``
        or the tool's DB config row.  Defaults to the cloud endpoint.
    timeout :
        HTTP request timeout in seconds (default: 30).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key or ""
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None

    # ── Context manager ────────────────────────────────────────────────────

    async def __aenter__(self) -> "GravityZoneClient":
        self._http = self._build_http()
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
        if self._http:
            await self._http.aclose()
            self._http = None

    # ── Internal ───────────────────────────────────────────────────────────

    def _build_http(self) -> httpx.AsyncClient:
        """Build an httpx client with Basic Auth pre-configured."""
        if not self._api_key:
            raise GravityZoneError(
                "GravityZone API key is not configured.",
                code=-32001,
            )
        # GravityZone Basic Auth: api_key as username, empty password
        raw = f"{self._api_key}:".encode()
        b64 = base64.b64encode(raw).decode()
        return httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {b64}",
            },
        )

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = self._build_http()
        return self._http

    # ── Core JSON-RPC call ─────────────────────────────────────────────────

    async def call(
        self,
        service: str,
        method: str,
        params: Optional[dict] = None,
        *,
        api_version: str = "v1.0",
    ) -> Any:
        """Execute a single GravityZone JSON-RPC method.

        Parameters
        ----------
        service :
            API service path segment (e.g. ``"network"``, ``"incidents"``,
            ``"phasr"``).  Appended to the versioned API URL.
        method :
            JSON-RPC method name (e.g. ``"getEndpointsList"``).
        params :
            Method parameters dict (optional).
        api_version :
            API version to use (default: ``"v1.0"``).  Use ``"v1.1"`` or
            ``"v1.2"`` for methods that offer improved capabilities in newer
            versions (e.g. ``addToBlocklist`` v1.2 supports path/connection
            rules; ``createIsolateEndpointTask`` v1.1 returns task IDs).

        Returns
        -------
        Any
            The ``result`` value from the JSON-RPC response.

        Raises
        ------
        GravityZoneError
            On API errors, HTTP failures, or network issues.
        """
        if not self._base_url:
            raise GravityZoneError("GravityZone base URL is not configured.", code=-32001)

        url = f"{self._base_url}/{api_version}/jsonrpc/{service}"
        request_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": request_id,
        }
        if params:
            payload["params"] = params

        log.debug(
            "GravityZone RPC: service=%s method=%s version=%s id=%s params=%s",
            service, method, api_version, request_id, params,
        )

        client = self._get_http()
        try:
            resp = await client.post(url, json=payload)
        except httpx.RequestError as exc:
            raise GravityZoneError(
                f"Network error calling {service}/{method}: {exc}",
                code=-32002,
            ) from exc

        # Parse JSON regardless of HTTP status (GravityZone can return 200 with error body)
        try:
            body = resp.json()
        except Exception:
            raise GravityZoneError(
                f"Invalid JSON response from GravityZone (HTTP {resp.status_code}): {resp.text[:300]}",
                status_code=resp.status_code,
                code=-32003,
            )

        # HTTP error without parseable JSON-RPC error — surface it
        if not resp.is_success and "error" not in body:
            raise GravityZoneError(
                f"GravityZone HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
            )

        # JSON-RPC application error
        rpc_error = body.get("error")
        if rpc_error:
            err_code = rpc_error.get("code", 0) if isinstance(rpc_error, dict) else 0
            err_msg = (
                rpc_error.get("message", str(rpc_error))
                if isinstance(rpc_error, dict)
                else str(rpc_error)
            )
            raise GravityZoneError(
                f"{service}/{method} error: {err_msg}",
                status_code=resp.status_code,
                code=err_code,
            )

        return body.get("result")

    # ── Pagination helper ──────────────────────────────────────────────────

    async def call_paginated(
        self,
        service: str,
        method: str,
        params: Optional[dict] = None,
        *,
        per_page: int = 100,
        max_pages: int = 10,
        api_version: str = "v1.0",
    ) -> list[Any]:
        """Call a paginated GravityZone method and aggregate all items.

        Iterates pages starting from 1, stopping when ``hasMoreRecords``
        is ``False`` or ``max_pages`` is reached.

        Parameters
        ----------
        service, method :
            Same as :meth:`call`.
        params :
            Base parameters dict; ``page`` and ``perPage`` are injected/overridden.
        per_page :
            Items per page (default: 100, max enforced by GravityZone is 1000).
        max_pages :
            Safety cap on number of page fetches (default: 10 = up to 1000 items).
        api_version :
            API version to pass through to each :meth:`call` invocation.

        Returns
        -------
        list
            Aggregated ``items`` lists from all pages.
        """
        base_params: dict[str, Any] = dict(params or {})
        base_params["perPage"] = per_page

        all_items: list[Any] = []
        for page in range(1, max_pages + 1):
            base_params["page"] = page
            result = await self.call(service, method, base_params, api_version=api_version)
            if not isinstance(result, dict):
                break
            items = result.get("items", [])
            if isinstance(items, list):
                all_items.extend(items)
            if not result.get("hasMoreRecords", False):
                break

        return all_items
