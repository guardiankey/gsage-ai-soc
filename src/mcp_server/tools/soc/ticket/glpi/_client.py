"""gSage AI — GLPI REST API client.

Provides a thin async wrapper over the GLPI REST API.
Manages session tokens (initSession / killSession) transparently
and exposes the core CRUD / search operations used by the GLPI tools.

Authentication
--------------
Only ``user_token`` authentication is supported.  Provide the token
via the constructor arguments.  Configure these via the
tool config (``TOOL_{TOOL_NAME}__URL`` / ``TOOL_{TOOL_NAME}__USER_TOKEN``
env vars or the GSageToolConfig DB row).

Usage
-----
::

    async with GLPIClient(url=..., user_token=...) as client:
        ticket = await client.get_item("Ticket", 42)
        results = await client.search_items("Ticket", criteria=[...])
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0


def _mask_token(token: str) -> str:
    """Return a masked preview of a token for safe logging (first 4 + last 4 chars)."""
    if not token:
        return "<empty>"
    if len(token) <= 10:
        return f"{token[:2]}***({len(token)} chars)"
    return f"{token[:4]}…{token[-4:]}({len(token)} chars)"


class GLPIError(Exception):
    """Raised when the GLPI API returns an error response.

    Attributes
    ----------
    status_code : int
        HTTP status code of the response, or 0 for connection/parse errors.
    glpi_error : str
        The GLPI error code (e.g. ``"ERROR_SESSION_TOKEN_INVALID"``).
    message : str
        Human-readable error description from GLPI.
    """

    def __init__(self, message: str, status_code: int = 0, glpi_error: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.glpi_error = glpi_error


class GLPIClient:
    """Async GLPI REST API client.

    Parameters
    ----------
    url :
        Base URL of the GLPI apirest.php endpoint.
        Configure via ``TOOL_{TOOL_NAME}__URL`` or the tool's DB config row.
    user_token :
        User token from GLPI personal preferences (remote access key).
        Configure via ``TOOL_{TOOL_NAME}__USER_TOKEN`` or the tool's DB config row.
    app_token :
        Application token configured in GLPI API settings.
        Configure via ``TOOL_{TOOL_NAME}__APP_TOKEN`` or the tool's DB config row.  Optional.
    timeout :
        HTTP request timeout in seconds (default: 30).
    """

    def __init__(
        self,
        url: Optional[str] = None,
        user_token: Optional[str] = None,
        app_token: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._url = (url or "").rstrip("/")
        self._user_token = user_token or ""
        self._app_token = app_token or ""
        self._timeout = timeout
        self._session_token: Optional[str] = None
        self._http: Optional[httpx.AsyncClient] = None

        # Diagnostic: log resolved configuration
        log.info(
            "GLPIClient init: url=%s, user_token=%s (len=%d), app_token=%s",
            self._url or "<empty>",
            _mask_token(self._user_token),
            len(self._user_token),
            "present" if self._app_token else "absent",
        )

    # ── Context manager ────────────────────────────────────────────────────

    async def __aenter__(self) -> "GLPIClient":
        self._http = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={"Content-Type": "application/json"},
        )
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        await self.close()

    # ── Session management ─────────────────────────────────────────────────

    async def _ensure_session(self) -> None:
        """Obtain and cache a GLPI session token if not already authenticated."""
        if self._session_token:
            return
        if not self._url:
            raise GLPIError("GLPI URL is not configured.", glpi_error="CONFIG_MISSING")
        if not self._user_token:
            raise GLPIError("GLPI user token is not configured.", glpi_error="CONFIG_MISSING")

        headers = {"Authorization": f"user_token {self._user_token}"}
        if self._app_token:
            headers["App-Token"] = self._app_token

        init_url = f"{self._url}/initSession"
        log.info(
            "GLPI initSession: url=%s, user_token=%s, app_token=%s",
            init_url,
            _mask_token(self._user_token),
            "present" if self._app_token else "absent",
        )

        client = self._get_http()
        try:
            resp = await client.get(init_url, headers=headers)
        except httpx.RequestError as exc:
            raise GLPIError(
                f"Failed to connect to GLPI at {self._url}: {exc}",
                glpi_error="CONNECTION_ERROR",
            ) from exc

        if resp.status_code != 200:
            log.warning(
                "GLPI initSession failed: status=%d, body=%s, url=%s, user_token=%s",
                resp.status_code,
                resp.text[:500],
                init_url,
                _mask_token(self._user_token),
            )
            _raise_from_response(resp, "initSession failed")

        data = resp.json()
        self._session_token = data.get("session_token")
        if not self._session_token:
            raise GLPIError(
                "GLPI initSession returned no session_token.",
                status_code=resp.status_code,
                glpi_error="SESSION_INIT_FAILED",
            )
        log.debug("GLPI session initialized (token=%s)", _mask_token(self._session_token))

    async def close(self) -> None:
        """Kill the GLPI session (if active) and close the HTTP client."""
        if self._session_token and self._http:
            try:
                await self._http.get(
                    f"{self._url}/killSession",
                    headers=self._session_headers(),
                )
            except Exception:
                pass  # best-effort
            finally:
                self._session_token = None
        if self._http:
            await self._http.aclose()
            self._http = None

    # ── Generic HTTP ───────────────────────────────────────────────────────

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            # Support usage without context manager (tools manage lifecycle via close())
            self._http = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"Content-Type": "application/json"},
            )
        return self._http

    def _session_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Session-Token": self._session_token or ""}
        if self._app_token:
            headers["App-Token"] = self._app_token
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json: Optional[Any] = None,
        retry_on_401: bool = True,
    ) -> Any:
        """Send an authenticated request; re-authenticates once on 401."""
        await self._ensure_session()
        client = self._get_http()
        url = f"{self._url}/{path.lstrip('/')}"

        try:
            resp = await client.request(
                method,
                url,
                headers=self._session_headers(),
                params=params,
                json=json,
            )
        except httpx.RequestError as exc:
            raise GLPIError(
                f"Network error during {method} {path}: {exc}",
                glpi_error="CONNECTION_ERROR",
            ) from exc

        # Re-authenticate on session expiry and retry once
        if resp.status_code == 401 and retry_on_401:
            log.debug("GLPI session expired, re-authenticating")
            self._session_token = None
            await self._ensure_session()
            try:
                resp = await client.request(
                    method,
                    url,
                    headers=self._session_headers(),
                    params=params,
                    json=json,
                )
            except httpx.RequestError as exc:
                raise GLPIError(
                    f"Network error during retry of {method} {path}: {exc}",
                    glpi_error="CONNECTION_ERROR",
                ) from exc

        if not resp.is_success:
            _raise_from_response(resp, f"{method} {path}")

        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ── Public API ─────────────────────────────────────────────────────────

    async def get_item(
        self,
        itemtype: str,
        item_id: int,
        *,
        expand_dropdowns: bool = True,
        with_tickets: bool = False,
        with_devices: bool = False,
        with_softwares: bool = False,
        with_connections: bool = False,
        with_networkports: bool = False,
        with_infocoms: bool = False,
        with_contracts: bool = False,
        with_documents: bool = False,
    ) -> dict:
        """Fetch a single item by itemtype and ID.

        Returns the item dict from GLPI.
        """
        p: dict[str, Any] = {"expand_dropdowns": str(expand_dropdowns).lower()}
        if with_tickets:
            p["with_tickets"] = "true"
        if with_devices:
            p["with_devices"] = "true"
        if with_softwares:
            p["with_softwares"] = "true"
        if with_connections:
            p["with_connections"] = "true"
        if with_networkports:
            p["with_networkports"] = "true"
        if with_infocoms:
            p["with_infocoms"] = "true"
        if with_contracts:
            p["with_contracts"] = "true"
        if with_documents:
            p["with_documents"] = "true"

        result = await self._request("GET", f"{itemtype}/{item_id}", params=p)
        return result or {}

    async def get_item_no_links(
        self,
        itemtype: str,
        item_id: int,
        *,
        expand_dropdowns: bool = True,
    ) -> dict:
        """Fetch a single item **without** HATEOAS links.

        Used internally when resolving related items (User, Entity, etc.)
        to avoid embedding another full ``links`` array in each resolved entry.
        """
        p: dict[str, Any] = {
            "expand_dropdowns": str(expand_dropdowns).lower(),
            "get_hateoas": "false",
        }
        result = await self._request("GET", f"{itemtype}/{item_id}", params=p)
        return result or {}

    async def download_document(self, doc_id: int) -> tuple[bytes, str, str]:
        """Download a GLPI document as raw bytes.

        Calls ``GET /Document/{doc_id}?alt=media`` and returns
        ``(raw_bytes, filename, content_type)``.

        The filename is extracted from the ``Content-Disposition`` header
        when available; otherwise falls back to ``document_{doc_id}``.
        """
        await self._ensure_session()
        client = self._get_http()
        url = f"{self._url}/Document/{doc_id}"

        try:
            resp = await client.get(
                url,
                headers=self._session_headers(),
                params={"alt": "media"},
            )
        except httpx.RequestError as exc:
            raise GLPIError(
                f"Network error downloading document {doc_id}: {exc}",
                glpi_error="CONNECTION_ERROR",
            ) from exc

        if resp.status_code == 401:
            # Re-authenticate and retry once
            self._session_token = None
            await self._ensure_session()
            try:
                resp = await client.get(
                    url,
                    headers=self._session_headers(),
                    params={"alt": "media"},
                )
            except httpx.RequestError as exc:
                raise GLPIError(
                    f"Network error downloading document {doc_id} (retry): {exc}",
                    glpi_error="CONNECTION_ERROR",
                ) from exc

        if not resp.is_success:
            _raise_from_response(resp, f"GET Document/{doc_id}")

        raw = resp.content
        filename = f"document_{doc_id}"
        content_type = resp.headers.get("Content-Type", "application/octet-stream")

        # Try to extract filename from Content-Disposition header
        cd = resp.headers.get("Content-Disposition", "")
        if cd:
            import re as _re
            match = _re.search(r'filename[^;=\n]*=["\']?([^"\';\n]*)', cd, _re.IGNORECASE)
            if match:
                filename = match.group(1).strip() or filename

        return raw, filename, content_type

    async def get_all_items(
        self,
        itemtype: str,
        *,
        range: str = "0-49",
        sort: Optional[int] = None,
        order: str = "ASC",
        search_text: Optional[dict[str, str]] = None,
        is_deleted: bool = False,
        expand_dropdowns: bool = True,
    ) -> list[dict]:
        """Return a collection of items for the given itemtype."""
        p: dict[str, Any] = {
            "range": range,
            "order": order,
            "expand_dropdowns": str(expand_dropdowns).lower(),
        }
        if sort is not None:
            p["sort"] = sort
        if is_deleted:
            p["is_deleted"] = "true"
        if search_text:
            for field, value in search_text.items():
                p[f"searchText[{field}]"] = value

        result = await self._request("GET", f"{itemtype}/", params=p)
        if isinstance(result, list):
            return result
        return []

    async def get_sub_items(
        self,
        itemtype: str,
        item_id: int,
        sub_itemtype: str,
        *,
        range: str = "0-49",
        expand_dropdowns: bool = True,
    ) -> list[dict]:
        """Return sub-items for a given parent item."""
        p: dict[str, Any] = {
            "range": range,
            "expand_dropdowns": str(expand_dropdowns).lower(),
        }
        result = await self._request(
            "GET", f"{itemtype}/{item_id}/{sub_itemtype}", params=p
        )
        if isinstance(result, list):
            return result
        return []

    async def search_items(
        self,
        itemtype: str,
        criteria: list[dict],
        *,
        metacriteria: Optional[list[dict]] = None,
        range: str = "0-49",
        sort: Optional[int] = None,
        order: str = "ASC",
        forcedisplay: Optional[list[int]] = None,
    ) -> dict:
        """Search items using GLPI's search engine.

        Returns a dict with keys: ``totalcount``, ``count``, ``data``.
        ``data`` is a list of dicts keyed by searchOption IDs.
        """
        p: dict[str, Any] = {
            "range": range,
            "order": order,
            "uid_cols": "false",
            "giveItems": "false",
        }
        if sort is not None:
            p["sort"] = sort

        # Encode criteria as indexed query params
        for i, crit in enumerate(criteria):
            for k, v in crit.items():
                p[f"criteria[{i}][{k}]"] = str(v)

        if metacriteria:
            for i, mc in enumerate(metacriteria):
                for k, v in mc.items():
                    p[f"metacriteria[{i}][{k}]"] = str(v)

        if forcedisplay:
            for i, field_id in enumerate(forcedisplay):
                p[f"forcedisplay[{i}]"] = str(field_id)

        result = await self._request("GET", f"search/{itemtype}/", params=p)
        if isinstance(result, dict):
            # Normalise: data can be a dict (indexed by id) or list
            raw_data = result.get("data", {})
            if isinstance(raw_data, dict):
                result["data"] = list(raw_data.values())
            return result
        return {"totalcount": 0, "count": 0, "data": []}

    async def list_search_options(self, itemtype: str) -> dict:
        """Return all available search options (fields) for a given itemtype.

        Calls ``GET /listSearchOptions/{itemtype}?raw`` and returns the raw
        GLPI dict, where keys are integer field IDs (as strings) or special
        labels like ``"common"``.

        This is useful for discovering the ``field`` IDs required by
        :meth:`search_items` criteria.
        """
        result = await self._request(
            "GET",
            f"listSearchOptions/{itemtype}",
            params={"raw": ""},
        )
        return result if isinstance(result, dict) else {}

    async def add_item(self, itemtype: str, input_data: dict) -> dict:
        """Add a new item to GLPI.

        Returns a dict ``{"id": <int>, "message": <str>}``.
        """
        result = await self._request("POST", f"{itemtype}/", json={"input": input_data})
        if isinstance(result, (list, dict)):
            if isinstance(result, list) and result:
                return result[0]
            if isinstance(result, dict):
                return result
        return {}

    async def update_item(
        self, itemtype: str, item_id: int, input_data: dict
    ) -> dict:
        """Update an existing item.

        Returns the GLPI update result dict.
        """
        result = await self._request(
            "PUT", f"{itemtype}/{item_id}", json={"input": input_data}
        )
        # GLPI returns [{item_id: true/false, message: ""}]
        if isinstance(result, list) and result:
            return result[0]
        if isinstance(result, dict):
            return result
        return {}

    async def delete_item(
        self,
        itemtype: str,
        item_id: int,
        *,
        force_purge: bool = False,
    ) -> dict:
        """Delete an item.

        By default GLPI moves the item to trash; pass ``force_purge=True`` to
        permanently purge it (required for pivot tables like
        ``Ticket_User`` / ``Group_Ticket`` that don't support soft-delete).

        Returns the GLPI delete result dict.
        """
        params: dict[str, Any] = {}
        if force_purge:
            params["force_purge"] = "true"
        result = await self._request(
            "DELETE", f"{itemtype}/{item_id}", params=params or None
        )
        if isinstance(result, list) and result:
            return result[0]
        if isinstance(result, dict):
            return result
        return {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _raise_from_response(resp: httpx.Response, context: str) -> None:
    """Parse a GLPI error response and raise :class:`GLPIError`."""
    glpi_error = ""
    message = f"GLPI API error (HTTP {resp.status_code})"
    try:
        body = resp.json()
        if isinstance(body, list) and len(body) >= 2:
            # GLPI error format: ["ERROR_CODE", "human message"]
            glpi_error = str(body[0])
            message = str(body[1])
        elif isinstance(body, dict):
            glpi_error = body.get("error", "")
            message = body.get("message", message)
    except Exception:
        pass
    raise GLPIError(
        f"{context}: {message}",
        status_code=resp.status_code,
        glpi_error=glpi_error,
    )
