"""Async client for an Imperva SecureSphere 15.x Management Server REST API."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Optional

import httpx


IMPERVA_WAF_CONFIG_SCHEMA: dict = {
    "type": "object",
    "required": ["host", "username", "password"],
    "properties": {
        "host": {"type": "string", "description": "SecureSphere Management Server host or URL."},
        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
        "username": {"type": "string", "description": "SecureSphere API username."},
        "password": {"type": "string", "description": "SecureSphere API password (sensitive)."},
        "api_base_path": {"type": "string", "description": "SecureSphere REST API base path."},
        "verify_ssl": {"type": "boolean", "description": "Verify TLS certificates."},
        "timeout": {"type": "integer", "minimum": 5, "maximum": 300},
    },
    "additionalProperties": False,
}
IMPERVA_WAF_CONFIG_DEFAULTS = {"host": "", "port": 443, "username": "", "password": "", "api_base_path": "/api/v1", "verify_ssl": True, "timeout": 30}


class ImpervaWafError(Exception):
    def __init__(self, message: str, code: str = "IMPERVA_WAF_ERROR", status_code: int = 0) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code


_HTTP_CODES = {400: "INVALID_PARAMS", 401: "AUTH_ERROR", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT", 429: "RATE_LIMITED"}


class ImpervaWafClient:
    def __init__(self, config: dict) -> None:
        host = str(config.get("host") or "").strip().rstrip("/")
        self._base_url = host if host.startswith("https://") or host.startswith("http://") else f"https://{host}:{int(config.get('port') or 443)}"
        self._api_base_path = "/" + str(config.get("api_base_path") or "/api/v1").strip("/")
        self._username, self._password = str(config.get("username") or "").strip(), str(config.get("password") or "")
        self._verify_ssl, self._timeout = bool(config.get("verify_ssl", True)), float(config.get("timeout") or 30)
        self._http: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "ImpervaWafClient":
        if not self._base_url or not self._username or not self._password:
            raise ImpervaWafError("SecureSphere config requires host, username and password.", "CONFIG_MISSING")
        self._http = httpx.AsyncClient(base_url=f"{self._base_url}{self._api_base_path}", auth=(self._username, self._password), verify=self._verify_ssl, timeout=self._timeout, headers={"Accept": "application/json"})
        return self

    async def __aexit__(self, exc_type: Optional[type[BaseException]], exc_val: Optional[BaseException], exc_tb: Optional[TracebackType]) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def request(self, method: str, path: str, *, payload: Optional[dict] = None) -> Any:
        if self._http is None:
            raise ImpervaWafError("Client must be used as an async context manager.")
        try:
            response = await self._http.request(method, path, json=payload)
        except httpx.TimeoutException as exc:
            raise ImpervaWafError(f"SecureSphere request timed out: {exc}", "TIMEOUT") from exc
        except httpx.TransportError as exc:
            raise ImpervaWafError(f"SecureSphere connection error: {exc}", "CONNECTION_ERROR") from exc
        if response.status_code >= 400:
            raise ImpervaWafError(response.text[:1000], _HTTP_CODES.get(response.status_code, "UPSTREAM_ERROR"), response.status_code)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ImpervaWafError("SecureSphere returned invalid JSON.", "UPSTREAM_ERROR", response.status_code) from exc


def build_imperva_waf_client(config: dict) -> ImpervaWafClient:
    return ImpervaWafClient(config)
