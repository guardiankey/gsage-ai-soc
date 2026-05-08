"""gSage AI — GitLab REST API v4 async client.

Thin ``httpx``-based wrapper that:

- Normalises configuration (base URL, Personal Access Token, verify_ssl,
  timeout, proxy).
- Authenticates via ``PRIVATE-TOKEN`` header (GitLab PAT).
- Abstracts paginated GET requests (``get_paginated``) so callers only
  need to specify the path, query params and an item cap.
- Maps HTTP / network errors to a stable :class:`GitLabError` with a
  machine-readable ``code``.

Usage::

    async with build_gitlab_client(config) as client:
        projects = await client.get_paginated("/projects", {"search": "api"}, max_items=50)

Authentication: GitLab Personal Access Token via
``PRIVATE-TOKEN: <token>`` header.  Tokens need at minimum ``read_api`` scope
for the read tool and ``api`` scope for the manage tool.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Optional
from urllib.parse import quote_plus

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration schema / defaults (used by both tools via config_schema)
# ---------------------------------------------------------------------------

GITLAB_CONFIG_SCHEMA: dict = {
    "type": "object",
    "required": ["url", "token"],
    "properties": {
        "url": {
            "type": "string",
            "description": (
                "Base URL of the GitLab instance, e.g. 'https://gitlab.com' "
                "or 'https://git.yourcompany.com'.  Must NOT include /api/v4."
            ),
        },
        "token": {
            "type": "string",
            "description": (
                "Personal Access Token (PAT).  Needs 'read_api' scope for "
                "gitlab_read and 'api' scope for gitlab_manage."
            ),
        },
        "verify_ssl": {
            "type": "boolean",
            "description": "Verify TLS certificate of the GitLab server (default: true).",
        },
        "timeout": {
            "type": "integer",
            "minimum": 5,
            "maximum": 120,
            "description": "HTTP request timeout in seconds (default: 30).",
        },
        "proxy": {
            "type": "string",
            "description": "Optional HTTP/HTTPS proxy URL.",
        },
        "default_per_page": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "Default page size for paginated requests (default: 20).",
        },
    },
    "additionalProperties": False,
}

GITLAB_CONFIG_DEFAULTS: dict = {
    "url": "",
    "token": "",
    "verify_ssl": True,
    "timeout": 30,
    "proxy": "",
    "default_per_page": 20,
}

_MAX_ITEMS_HARD_CAP = 500


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------

class GitLabError(Exception):
    """Raised for GitLab API or transport errors.

    Attributes
    ----------
    code:
        Stable agent-friendly error code:
        ``AUTH_ERROR`` | ``FORBIDDEN`` | ``NOT_FOUND`` | ``RATE_LIMITED`` |
        ``INVALID_PARAMS`` | ``UPSTREAM_ERROR`` | ``CONNECTION_ERROR`` |
        ``CONFIG_MISSING`` | ``GITLAB_ERROR``.
    status_code:
        HTTP status code; 0 for transport / config errors.
    """

    def __init__(
        self,
        message: str,
        code: str = "GITLAB_ERROR",
        status_code: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mask_token(token: str) -> str:
    if not token:
        return "<empty>"
    if len(token) <= 10:
        return f"{token[:2]}***({len(token)} chars)"
    return f"{token[:4]}…{token[-4:]}({len(token)} chars)"


def encode_project_id(project: str | int) -> str:
    """Return the URL-encoded project identifier.

    GitLab accepts both numeric IDs and ``namespace/path`` strings.
    Numeric IDs are passed through as-is; strings are URL-encoded so that
    slashes become ``%2F``.
    """
    if isinstance(project, int) or (isinstance(project, str) and project.isdigit()):
        return str(project)
    return quote_plus(str(project))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class GitLabClient:
    """Async GitLab REST API v4 client.

    Parameters
    ----------
    url:
        Base URL of the GitLab instance (without ``/api/v4``).
    token:
        Personal Access Token.
    verify_ssl:
        Whether to verify TLS certificates.
    timeout:
        HTTP request timeout in seconds.
    proxy:
        Optional proxy URL.
    default_per_page:
        Default page size for paginated requests.

    Usage::

        async with GitLabClient(url=..., token=...) as client:
            items = await client.get_paginated("/projects", max_items=50)
    """

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        verify_ssl: bool = True,
        timeout: float = 30.0,
        proxy: Optional[str] = None,
        default_per_page: int = 20,
    ) -> None:
        self._base_url = (url or "").rstrip("/")
        self._token = token or ""
        self._verify_ssl = verify_ssl
        self._timeout = float(timeout) if timeout else 30.0
        self._proxy = proxy or None
        self._default_per_page = max(1, min(100, int(default_per_page or 20)))
        self._http: Optional[httpx.AsyncClient] = None

    # ── Context manager ─────────────────────────────────────────────────────

    async def __aenter__(self) -> "GitLabClient":
        self._ensure_configured()
        proxies = {"all://": self._proxy} if self._proxy else None
        self._http = httpx.AsyncClient(
            base_url=f"{self._base_url}/api/v4",
            headers={
                "PRIVATE-TOKEN": self._token,
                "Accept": "application/json",
            },
            verify=self._verify_ssl,
            timeout=self._timeout,
            proxy=self._proxy if self._proxy else None,  # type: ignore[arg-type]
        )
        _ = proxies  # unused when passed via proxy= kwarg
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    # ── Internal helpers ────────────────────────────────────────────────────

    def _ensure_configured(self) -> None:
        if not self._base_url:
            raise GitLabError("GitLab URL is not configured.", code="CONFIG_MISSING")
        if not self._token:
            raise GitLabError("GitLab token is not configured.", code="CONFIG_MISSING")

    def _http_or_error(self) -> httpx.AsyncClient:
        if self._http is None:
            raise GitLabError(
                "GitLabClient must be used as an async context manager.",
                code="GITLAB_ERROR",
            )
        return self._http

    def _translate(self, exc: BaseException) -> GitLabError:
        """Map an httpx or HTTP-level error to :class:`GitLabError`."""
        if isinstance(exc, GitLabError):
            return exc
        if isinstance(exc, httpx.TimeoutException):
            return GitLabError(
                f"Request timed out after {self._timeout}s: {exc}",
                code="CONNECTION_ERROR",
            )
        if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
            return GitLabError(
                f"Could not connect to GitLab at {self._base_url}: {exc}",
                code="CONNECTION_ERROR",
            )
        if isinstance(exc, httpx.HTTPStatusError):
            sc = exc.response.status_code
            try:
                body = exc.response.json()
                msg = (
                    body.get("message")
                    or body.get("error")
                    or exc.response.text[:200]
                )
            except Exception:
                msg = exc.response.text[:200]
            code_map = {
                401: "AUTH_ERROR",
                403: "FORBIDDEN",
                404: "NOT_FOUND",
                422: "INVALID_PARAMS",
                429: "RATE_LIMITED",
            }
            code = code_map.get(sc, "UPSTREAM_ERROR")
            return GitLabError(msg, code=code, status_code=sc)
        return GitLabError(str(exc), code="GITLAB_ERROR")

    async def _get(self, path: str, params: Optional[dict] = None) -> Any:
        """Single GET request; returns parsed JSON."""
        http = self._http_or_error()
        try:
            resp = await http.get(path, params=params or {})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            raise self._translate(exc) from exc
        except Exception as exc:
            raise self._translate(exc) from exc

    # ── Public paginated helper ──────────────────────────────────────────────

    async def get_paginated(
        self,
        path: str,
        params: Optional[dict] = None,
        max_items: int = 20,
    ) -> list[dict]:
        """Fetch all pages for *path* up to *max_items* items.

        Uses the ``X-Next-Page`` response header to follow pages.
        Hard-capped at :data:`_MAX_ITEMS_HARD_CAP`.

        Returns a flat list of items.
        """
        cap = min(max_items, _MAX_ITEMS_HARD_CAP)
        per_page = min(self._default_per_page, cap, 100)
        http = self._http_or_error()
        p = {**(params or {}), "per_page": per_page, "page": 1}
        items: list[dict] = []

        while True:
            try:
                resp = await http.get(path, params=p)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise self._translate(exc) from exc
            except Exception as exc:
                raise self._translate(exc) from exc

            batch = resp.json()
            if isinstance(batch, list):
                items.extend(batch)
            else:
                # Single object returned (shouldn't happen for list endpoints)
                items.append(batch)
                break

            if len(items) >= cap:
                items = items[:cap]
                break

            next_page = resp.headers.get("X-Next-Page", "")
            if not next_page:
                break
            p["page"] = int(next_page)

        return items

    # ── Single-resource GET helpers ──────────────────────────────────────────

    async def get_project(self, project: str | int) -> dict:
        pid = encode_project_id(project)
        return await self._get(f"/projects/{pid}")  # type: ignore[return-value]

    async def get_user(self, user_id: int) -> dict:
        return await self._get(f"/users/{user_id}")  # type: ignore[return-value]

    async def get_issue(self, project: str | int, issue_iid: int) -> dict:
        pid = encode_project_id(project)
        return await self._get(f"/projects/{pid}/issues/{issue_iid}")  # type: ignore[return-value]

    # ── Write operations ─────────────────────────────────────────────────────

    async def create_issue(self, project: str | int, payload: dict) -> dict:
        pid = encode_project_id(project)
        http = self._http_or_error()
        try:
            resp = await http.post(f"/projects/{pid}/issues", json=payload)
            resp.raise_for_status()
            return resp.json()  # type: ignore[return-value]
        except httpx.HTTPStatusError as exc:
            raise self._translate(exc) from exc
        except Exception as exc:
            raise self._translate(exc) from exc

    async def update_issue(
        self, project: str | int, issue_iid: int, payload: dict
    ) -> dict:
        pid = encode_project_id(project)
        http = self._http_or_error()
        try:
            resp = await http.put(
                f"/projects/{pid}/issues/{issue_iid}", json=payload
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[return-value]
        except httpx.HTTPStatusError as exc:
            raise self._translate(exc) from exc
        except Exception as exc:
            raise self._translate(exc) from exc

    async def create_note(
        self, project: str | int, issue_iid: int, body: str
    ) -> dict:
        pid = encode_project_id(project)
        http = self._http_or_error()
        try:
            resp = await http.post(
                f"/projects/{pid}/issues/{issue_iid}/notes",
                json={"body": body},
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[return-value]
        except httpx.HTTPStatusError as exc:
            raise self._translate(exc) from exc
        except Exception as exc:
            raise self._translate(exc) from exc


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_gitlab_client(config: dict) -> GitLabClient:
    """Build a :class:`GitLabClient` from a (already-decrypted) config dict."""
    merged = {**GITLAB_CONFIG_DEFAULTS, **config}
    return GitLabClient(
        url=merged.get("url") or "",
        token=merged.get("token") or "",
        verify_ssl=bool(merged.get("verify_ssl", True)),
        timeout=float(merged.get("timeout") or 30),
        proxy=merged.get("proxy") or None,
        default_per_page=int(merged.get("default_per_page") or 20),
    )
