"""gSage AI — OPNsense async client wrapper (shared by all opnsense_* tools).

OPNsense exposes a REST API at ``https://{host}/api/<module>/<controller>/
<command>``. Authentication is an **API key + secret** sent as HTTP Basic
credentials (key = username, secret = password). This is stateless and
maps directly onto ``httpx.AsyncClient`` — fully async, no new dependency.

    # Dependency: httpx (already used across the codebase).

Configuration fields:

- ``host``: OPNsense FQDN or IP.
- ``port``: HTTPS port (default 443).
- ``api_key``: API key (the Basic-auth username).
- ``api_secret``: API secret (the Basic-auth password; sensitive).
- ``verify_ssl``: validate the TLS certificate (default true). Set false
  for the default self-signed OPNsense certificate.
- ``block_alias``: default firewall alias used by block_ip / unblock_ip
  (e.g. ``gsage_blocklist``). The alias must already exist and be
  referenced by a block rule.
- ``timeout``: per-request timeout in seconds (5–300, default 30).

Two important OPNsense API behaviours this client normalises:

1. **Soft validation errors** — write endpoints frequently return HTTP 200
   with a body like ``{"result": "failed", "validations": {...}}``. The
   :meth:`post` helper raises :class:`OPNsenseError` (``INVALID_PARAMS``)
   for these instead of returning a misleading success.
2. **Apply / reconfigure** — config changes are staged and only take
   effect after an apply call; helpers expose the relevant apply endpoints.
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

OPNSENSE_CONFIG_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "description": "OPNsense FQDN or IP address.",
        },
        "port": {
            "type": "integer",
            "minimum": 1,
            "maximum": 65535,
            "description": "OPNsense HTTPS port (default 443).",
        },
        "api_key": {
            "type": "string",
            "description": "OPNsense API key (Basic-auth username).",
        },
        "api_secret": {
            "type": "string",
            "description": "OPNsense API secret (Basic-auth password; sensitive).",
        },
        "verify_ssl": {
            "type": "boolean",
            "description": (
                "Validate the OPNsense TLS certificate (default true). Set "
                "false for the default self-signed certificate."
            ),
        },
        "block_alias": {
            "type": "string",
            "description": (
                "Default firewall alias for block_ip / unblock_ip (e.g. "
                "'gsage_blocklist'). Must exist and be referenced by a "
                "block rule."
            ),
        },
        "timeout": {
            "type": "integer",
            "minimum": 5,
            "maximum": 300,
            "description": "Per-request timeout in seconds (default 30).",
        },
    },
    "required": ["host", "api_key", "api_secret"],
    "additionalProperties": False,
}

OPNSENSE_CONFIG_DEFAULTS: dict = {
    "host": "",
    "port": 443,
    "api_key": "",
    "api_secret": "",
    "verify_ssl": True,
    "block_alias": "",
    "timeout": 30,
}


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class OPNsenseError(Exception):
    """Raised for OPNsense API / transport errors.

    Stable ``code`` values: ``AUTH_ERROR`` | ``FORBIDDEN`` | ``NOT_FOUND`` |
    ``CONFLICT`` | ``INVALID_PARAMS`` | ``RATE_LIMITED`` |
    ``CONNECTION_ERROR`` | ``TIMEOUT`` | ``CONFIG_MISSING`` |
    ``APPLY_FAILED`` | ``OPNSENSE_ERROR``.
    """

    def __init__(
        self,
        message: str,
        code: str = "OPNSENSE_ERROR",
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


def _translate(exc: BaseException) -> OPNsenseError:
    """Map an upstream httpx exception to :class:`OPNsenseError`."""
    if isinstance(exc, OPNsenseError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return OPNsenseError(f"OPNsense request timed out: {exc}", code="TIMEOUT")
    if isinstance(exc, httpx.ConnectError):
        return OPNsenseError(
            f"OPNsense connection error: {exc}", code="CONNECTION_ERROR"
        )
    if isinstance(exc, httpx.TransportError):
        return OPNsenseError(
            f"OPNsense transport error: {exc}", code="CONNECTION_ERROR"
        )
    return OPNsenseError(f"Unexpected OPNsense error: {exc}", code="OPNSENSE_ERROR")


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def build_opnsense_client(config: dict) -> "OPNsenseClient":
    """Build an :class:`OPNsenseClient` from a tool config dict.

    Use as an async context manager so the HTTP connection pool is closed::

        async with build_opnsense_client(config) as client:
            ...
    """
    return OPNsenseClient(config)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OPNsenseClient:
    """Async wrapper around the OPNsense REST API (httpx + API key/secret)."""

    def __init__(self, config: dict) -> None:
        self._cfg = dict(config or {})
        self._host = (self._cfg.get("host") or "").strip()
        self._port = int(self._cfg.get("port") or 443)
        self._key = (self._cfg.get("api_key") or "").strip()
        self._secret = (self._cfg.get("api_secret") or "").strip()
        self._verify_ssl = bool(self._cfg.get("verify_ssl", True))
        self._timeout = float(self._cfg.get("timeout") or 30)
        self._block_alias = (self._cfg.get("block_alias") or "").strip()
        self._http: Optional[httpx.AsyncClient] = None
        self._closed = False

    @property
    def host(self) -> str:
        return self._host

    @property
    def block_alias(self) -> str:
        return self._block_alias

    # ── Context manager ─────────────────────────────────────────────────────

    async def __aenter__(self) -> "OPNsenseClient":
        if not (self._host and self._key and self._secret):
            raise OPNsenseError(
                "OPNsense config missing required fields (host, api_key, "
                "api_secret).",
                code="CONFIG_MISSING",
            )
        self._http = httpx.AsyncClient(
            base_url=f"https://{self._host}:{self._port}/api",
            auth=(self._key, self._secret),
            verify=self._verify_ssl,
            timeout=self._timeout,
            headers={"Content-Type": "application/json"},
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
                log.debug("opnsense: error closing http client", exc_info=True)
            self._http = None

    # ── Core request ─────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise OPNsenseError(
                "OPNsenseClient must be used as an async context manager.",
                code="OPNSENSE_ERROR",
            )
        return self._http

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Any:
        try:
            resp = await self._client().request(
                method, path, params=params or None, json=json_body,
            )
        except Exception as exc:
            raise _translate(exc) from exc

        if resp.status_code >= 400:
            code = _HTTP_CODE_MAP.get(resp.status_code, "OPNSENSE_ERROR")
            raise OPNsenseError(
                f"{self._error_detail(resp)} (HTTP {resp.status_code})",
                code=code,
                status_code=resp.status_code,
            )
        try:
            return resp.json()
        except Exception:
            return resp.text

    @staticmethod
    def _error_detail(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            if isinstance(body, dict):
                if body.get("validations"):
                    parts = "; ".join(
                        f"{k}: {v}" for k, v in body["validations"].items()
                    )
                    return f"OPNsense validation error: {parts}"
                if body.get("message"):
                    return f"OPNsense error: {body['message']}"
        except Exception:
            pass
        text = (resp.text or "").strip()
        return f"OPNsense error: {text[:300]}" if text else "OPNsense error"

    # ── Verbs ────────────────────────────────────────────────────────────────

    async def get(self, path: str, **params: Any) -> Any:
        """GET an OPNsense endpoint; returns the parsed JSON body."""
        clean = {k: v for k, v in params.items() if v is not None}
        return await self._request("GET", path, params=clean)

    async def post(
        self, path: str, body: Optional[dict] = None, *, expect_apply: bool = False
    ) -> Any:
        """POST to an OPNsense action endpoint, surfacing soft failures.

        OPNsense often returns HTTP 200 with ``{"result": "failed", ...}``
        or ``{"validations": {...}}`` on bad input. This raises
        :class:`OPNsenseError` for those so callers see a real error.

        When ``expect_apply`` is set, a non-``ok`` ``status`` field (used by
        apply / reconfigure endpoints) is treated as ``APPLY_FAILED``.
        """
        result = await self._request("POST", path, json_body=body or {})
        if isinstance(result, dict):
            validations = result.get("validations")
            if validations:
                parts = "; ".join(f"{k}: {v}" for k, v in validations.items())
                raise OPNsenseError(
                    f"OPNsense validation error: {parts}", code="INVALID_PARAMS"
                )
            res = str(result.get("result") or "").lower()
            if res in ("failed", "error"):
                raise OPNsenseError(
                    f"OPNsense rejected the request: {result}",
                    code="INVALID_PARAMS",
                )
            if expect_apply:
                status = str(result.get("status") or "").lower()
                if status and status not in ("ok", "done", ""):
                    raise OPNsenseError(
                        f"OPNsense apply failed: {result}", code="APPLY_FAILED"
                    )
        return result

    # ── Common apply helpers ─────────────────────────────────────────────────

    async def apply_filter(self) -> Any:
        """Apply staged firewall filter (rule) changes."""
        return await self.post("/firewall/filter/apply", expect_apply=True)

    async def reconfigure_alias(self) -> Any:
        """Reload firewall aliases after an alias CRUD change."""
        return await self.post("/firewall/alias/reconfigure", expect_apply=True)


# Re-export for callers
__all__ = [
    "OPNSENSE_CONFIG_DEFAULTS",
    "OPNSENSE_CONFIG_SCHEMA",
    "OPNsenseClient",
    "OPNsenseError",
    "build_opnsense_client",
]
