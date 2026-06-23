"""Async client for the Imperva Cloud Application Security Provisioning API."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Optional

import httpx


IMPERVA_CLOUD_CONFIG_SCHEMA: dict = {
    "type": "object",
    "required": ["api_id", "api_key"],
    "properties": {
        "api_id": {"type": "string", "description": "Imperva Cloud API ID."},
        "api_key": {"type": "string", "description": "Imperva Cloud API key (sensitive)."},
        "base_url": {"type": "string", "description": "Provisioning API base URL."},
        "verify_ssl": {"type": "boolean", "description": "Verify TLS certificates."},
        "timeout": {"type": "integer", "minimum": 5, "maximum": 300},
    },
    "additionalProperties": False,
}
IMPERVA_CLOUD_CONFIG_DEFAULTS = {
    "api_id": "", "api_key": "", "base_url": "https://my.imperva.com/api/prov/v1",
    "verify_ssl": True, "timeout": 30,
}


class ImpervaCloudError(Exception):
    def __init__(self, message: str, code: str = "IMPERVA_CLOUD_ERROR", status_code: int = 0) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code


_HTTP_CODES = {400: "INVALID_PARAMS", 401: "AUTH_ERROR", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT", 429: "RATE_LIMITED"}


class ImpervaCloudClient:
    def __init__(self, config: dict) -> None:
        self._api_id = str(config.get("api_id") or "").strip()
        self._api_key = str(config.get("api_key") or "").strip()
        self._base_url = str(config.get("base_url") or IMPERVA_CLOUD_CONFIG_DEFAULTS["base_url"]).rstrip("/")
        self._verify_ssl = bool(config.get("verify_ssl", True))
        self._timeout = float(config.get("timeout") or 30)
        self._http: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "ImpervaCloudClient":
        if not self._api_id or not self._api_key:
            raise ImpervaCloudError("Imperva Cloud config requires api_id and api_key.", "CONFIG_MISSING")
        self._http = httpx.AsyncClient(base_url=self._base_url, verify=self._verify_ssl, timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type: Optional[type[BaseException]], exc_val: Optional[BaseException], exc_tb: Optional[TracebackType]) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def request(self, method: str, path: str, *, params: Optional[dict] = None, payload: Optional[dict] = None) -> Any:
        if self._http is None:
            raise ImpervaCloudError("Client must be used as an async context manager.")
        query = {"api_id": self._api_id, "api_key": self._api_key, **(params or {})}
        try:
            response = await self._http.request(method, path, params=query, json=payload)
        except httpx.TimeoutException as exc:
            raise ImpervaCloudError(f"Imperva Cloud request timed out: {exc}", "TIMEOUT") from exc
        except httpx.TransportError as exc:
            raise ImpervaCloudError(f"Imperva Cloud connection error: {exc}", "CONNECTION_ERROR") from exc
        if response.status_code >= 400:
            raise ImpervaCloudError(response.text[:1000], _HTTP_CODES.get(response.status_code, "UPSTREAM_ERROR"), response.status_code)
        try:
            data = response.json()
        except ValueError as exc:
            raise ImpervaCloudError("Imperva Cloud returned invalid JSON.", "UPSTREAM_ERROR", response.status_code) from exc
        # Provisioning API errors can be returned with a successful HTTP status.
        if isinstance(data, dict) and data.get("res") not in (None, 0, "0"):
            raise ImpervaCloudError(str(data.get("res_message") or data), "UPSTREAM_ERROR", response.status_code)
        return data


def build_imperva_cloud_client(config: dict) -> ImpervaCloudClient:
    return ImpervaCloudClient(config)
