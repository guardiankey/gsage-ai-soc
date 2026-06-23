"""gSage AI — SentinelOne Management API client (shared by all s1_* tools).

Thin async wrapper over the SentinelOne Management API (REST/JSON) using
``httpx``. Authentication is an **API token** sent in the standard
SentinelOne header::

    Authorization: ApiToken {api_token}

API tokens are generated in the SentinelOne console under
*Settings → Users → (user) → API Token* (or a Service User token).

    # Dependency: httpx (already used across the codebase).

Configuration fields:

- ``console_url``: management console base URL, e.g.
  ``https://usea1-partners.sentinelone.net``.
- ``api_token``: API token (sensitive).
- ``verify_ssl``: validate TLS (default true; S1 SaaS has valid certs).
- ``default_site_ids``: optional comma-separated site scope applied to
  list/blocklist calls when the caller omits one.
- ``timeout``: per-request timeout in seconds (5–300, default 30).
- ``api_version``: API path version (default ``v2.1``).

Usage::

    async with build_s1_client(config) as client:
        agents = await client.paginate("/agents", {"computerName__contains": "DC"})
        await client.post("/agents/actions/disconnect", {"filter": {"ids": [aid]}})

SentinelOne wraps list responses as ``{"data": [...], "pagination":
{"nextCursor": ..., "totalItems": N}}`` and single objects as
``{"data": {...}}``; errors come back as ``{"errors": [{...}]}`` — all
normalised here into return values / :class:`SentinelOneError`.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config schema / defaults
# ---------------------------------------------------------------------------

S1_CONFIG_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "console_url": {
            "type": "string",
            "description": (
                "SentinelOne console base URL, e.g. "
                "'https://usea1-partners.sentinelone.net'."
            ),
        },
        "api_token": {
            "type": "string",
            "description": "SentinelOne API token (sensitive).",
        },
        "verify_ssl": {
            "type": "boolean",
            "description": "Validate the TLS certificate (default true).",
        },
        "default_site_ids": {
            "type": "string",
            "description": (
                "Optional comma-separated site IDs used to scope list / "
                "blocklist calls when the caller omits 'site_ids'."
            ),
        },
        "timeout": {
            "type": "integer",
            "minimum": 5,
            "maximum": 300,
            "description": "Per-request timeout in seconds (default 30).",
        },
        "api_version": {
            "type": "string",
            "description": "API path version (default 'v2.1').",
        },
    },
    "required": ["console_url", "api_token"],
    "additionalProperties": False,
}

S1_CONFIG_DEFAULTS: dict = {
    "console_url": "",
    "api_token": "",
    "verify_ssl": True,
    "default_site_ids": "",
    "timeout": 30,
    "api_version": "v2.1",
}


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class SentinelOneError(Exception):
    """Raised for SentinelOne API / transport errors.

    Stable ``code`` values: ``AUTH_ERROR`` | ``FORBIDDEN`` | ``NOT_FOUND`` |
    ``CONFLICT`` | ``INVALID_PARAMS`` | ``RATE_LIMITED`` |
    ``CONNECTION_ERROR`` | ``TIMEOUT`` | ``CONFIG_MISSING`` |
    ``SENTINELONE_ERROR``.
    """

    def __init__(
        self,
        message: str,
        code: str = "SENTINELONE_ERROR",
        status_code: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


_HTTP_CODE_MAP = {
    400: "INVALID_PARAMS",
    401: "AUTH_ERROR",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    429: "RATE_LIMITED",
}


def _translate(exc: BaseException) -> SentinelOneError:
    """Map an upstream httpx exception to :class:`SentinelOneError`."""
    if isinstance(exc, SentinelOneError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return SentinelOneError(f"SentinelOne request timed out: {exc}", code="TIMEOUT")
    if isinstance(exc, httpx.ConnectError):
        return SentinelOneError(
            f"SentinelOne connection error: {exc}", code="CONNECTION_ERROR"
        )
    if isinstance(exc, httpx.TransportError):
        return SentinelOneError(
            f"SentinelOne transport error: {exc}", code="CONNECTION_ERROR"
        )
    return SentinelOneError(
        f"Unexpected SentinelOne error: {exc}", code="SENTINELONE_ERROR"
    )


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def build_s1_client(config: dict) -> "SentinelOneClient":
    """Build a :class:`SentinelOneClient` from a tool config dict.

    Use as an async context manager so the HTTP connection pool is closed.
    """
    return SentinelOneClient(config)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SentinelOneClient:
    """Async wrapper around the SentinelOne Management API (httpx + ApiToken)."""

    def __init__(self, config: dict) -> None:
        self._cfg = dict(config or {})
        self._console = (self._cfg.get("console_url") or "").strip().rstrip("/")
        self._token = (self._cfg.get("api_token") or "").strip()
        self._verify_ssl = bool(self._cfg.get("verify_ssl", True))
        self._timeout = float(self._cfg.get("timeout") or 30)
        self._api_version = (self._cfg.get("api_version") or "v2.1").strip()
        self._default_sites = (self._cfg.get("default_site_ids") or "").strip()
        self._http: Optional[httpx.AsyncClient] = None
        self._closed = False

    @property
    def console_url(self) -> str:
        return self._console

    @property
    def default_site_ids(self) -> str:
        return self._default_sites

    # ── Context manager ─────────────────────────────────────────────────────

    async def __aenter__(self) -> "SentinelOneClient":
        if not (self._console and self._token):
            raise SentinelOneError(
                "SentinelOne config missing required fields (console_url, "
                "api_token).",
                code="CONFIG_MISSING",
            )
        self._http = httpx.AsyncClient(
            base_url=f"{self._console}/web/api/{self._api_version}",
            headers={
                "Authorization": f"ApiToken {self._token}",
                "Content-Type": "application/json",
            },
            verify=self._verify_ssl,
            timeout=self._timeout,
        )
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        if self._closed:
            return
        self._closed = True
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                log.debug("s1: error closing http client", exc_info=True)
            self._http = None

    # ── Core request ─────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise SentinelOneError(
                "SentinelOneClient must be used as an async context manager.",
                code="SENTINELONE_ERROR",
            )
        return self._http

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> dict:
        clean_params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        try:
            resp = await self._client().request(
                method, path, params=clean_params or None, json=json_body,
            )
        except Exception as exc:
            raise _translate(exc) from exc

        body: Any = None
        try:
            body = resp.json()
        except Exception:
            body = None

        if resp.status_code >= 400:
            code = _HTTP_CODE_MAP.get(resp.status_code, "SENTINELONE_ERROR")
            raise SentinelOneError(
                f"{_error_detail(body, resp)} (HTTP {resp.status_code})",
                code=code,
                status_code=resp.status_code,
            )
        # S1 can return 200 with an "errors" array on some endpoints.
        if isinstance(body, dict) and body.get("errors"):
            raise SentinelOneError(
                _error_detail(body, resp), code="INVALID_PARAMS",
                status_code=resp.status_code,
            )
        return body if isinstance(body, dict) else {"data": body}

    # ── Verbs ────────────────────────────────────────────────────────────────

    async def get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET an endpoint; returns the full ``{data, pagination}`` envelope."""
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: Optional[dict] = None) -> dict:
        """POST a JSON body; returns the parsed envelope."""
        return await self._request("POST", path, json_body=body or {})

    async def delete(self, path: str, body: Optional[dict] = None) -> dict:
        return await self._request("DELETE", path, json_body=body or {})

    # ── Pagination ───────────────────────────────────────────────────────────

    async def paginate(
        self,
        path: str,
        params: Optional[dict] = None,
        *,
        limit: int = 100,
        max_items: int = 1000,
    ) -> list[dict]:
        """Aggregate a cursor-paginated list endpoint into a list of rows.

        Follows ``pagination.nextCursor`` until exhausted or ``max_items``.
        """
        out: list[dict] = []
        cursor: Optional[str] = None
        base = dict(params or {})
        base["limit"] = min(limit, max_items)
        while True:
            page_params = dict(base)
            if cursor:
                page_params["cursor"] = cursor
            body = await self.get(path, page_params)
            data = body.get("data")
            if isinstance(data, list):
                out.extend(data)
            if len(out) >= max_items:
                return out[:max_items]
            cursor = (body.get("pagination") or {}).get("nextCursor")
            if not cursor:
                return out

    def resolve_site_ids(self, params: Optional[dict]) -> Optional[str]:
        """Resolve a site scope from params or the profile default."""
        sites = ""
        if params:
            sites = (params.get("site_ids") or "").strip()
        return sites or (self._default_sites or None)


def _error_detail(body: Any, resp: httpx.Response) -> str:
    if isinstance(body, dict) and body.get("errors"):
        parts = []
        for e in body["errors"]:
            if isinstance(e, dict):
                parts.append(e.get("detail") or e.get("title") or str(e))
            else:
                parts.append(str(e))
        return f"SentinelOne error: {'; '.join(parts)[:400]}"
    text = (getattr(resp, "text", "") or "").strip()
    return f"SentinelOne error: {text[:300]}" if text else "SentinelOne error"


__all__ = [
    "S1_CONFIG_DEFAULTS",
    "S1_CONFIG_SCHEMA",
    "SentinelOneClient",
    "SentinelOneError",
    "build_s1_client",
]
