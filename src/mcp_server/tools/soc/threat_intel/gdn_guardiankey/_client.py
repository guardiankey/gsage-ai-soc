"""GuardianKey GDN — async REST client.

Adapts the synchronous ``GDNClient`` reference implementation (``ref_gk/gdn_api.py``)
to ``httpx.AsyncClient``, following the same async context-manager pattern used by
the other tool clients in this project.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

# Friendly name → full API path (resolved by get_dashboard_object_data)
OBJREFS: dict[str, str] = {
    "top_users":                   "m06_iap/auth_security/dashboards/obj_table_top_users",
    "top_clientips":               "m06_iap/auth_security/dashboards/obj_table_top_clientips",
    "events":                      "m06_iap/auth_security/dashboards/obj_table_events",
    "users":                       "m06_iap/auth_security/dashboards/obj_table_users",
    "top_clientips_users":         "m06_iap/auth_security/dashboards/obj_table_top_clientips_users",
    "top_users_cities":            "m06_iap/auth_security/dashboards/obj_table_top_users_cities",
    "areas_risk_treatment_in_time":"m06_iap/auth_security/dashboards/obj_areas_risk_treatment_in_time",
    "top_users_risk":              "m06_iap/auth_security/dashboards/obj_table_top_users_risk",
    "pie_event_responses":         "m06_iap/auth_security/dashboards/obj_pie_event_responses",
    "bars_events_in_time":         "m06_iap/auth_security/dashboards/obj_bars_events_in_time",
    "table_top_countries":         "m06_iap/auth_security/dashboards/obj_table_top_countries",
    "table_top_cities":            "m06_iap/auth_security/dashboards/obj_table_top_cities",
    "table_top_threats":           "m06_iap/auth_security/dashboards/obj_table_top_threats",
    "table_messagelog_events":     "m12_wap/dashboard/obj_table_messagelog_events",
}


class GDNError(Exception):
    """Raised when the GDN REST API returns an error response."""

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class GDNClient:
    """Async HTTP client for the GuardianKey GDN REST API.

    Usage::

        async with GDNClient(url="https://gdn.guardiankey.io", api_key="...") as client:
            data = await client.get_dashboard_object_data(orgid, "events", filters)
    """

    def __init__(
        self,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        raw_url = (url or "").strip()
        if not raw_url:
            raise GDNError(
                "GDN base URL is not configured. Set 'url' in the tool config "
                "(TOOL_GDN_GUARDIANKEY__URL or GSageToolConfig)."
            )

        # Ensure /api/v1 suffix
        if "api/v1" not in raw_url:
            raw_url = raw_url.rstrip("/") + "/api/v1"

        self._base_url = raw_url
        self._api_key = (api_key or "").strip()
        if not self._api_key:
            raise GDNError(
                "GDN API key is not configured. Set 'api_key' in the tool config "
                "(TOOL_GDN_GUARDIANKEY__API_KEY or GSageToolConfig)."
            )

        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None

    # ── Async context manager ─────────────────────────────────────────────

    async def __aenter__(self) -> "GDNClient":
        self._http = httpx.AsyncClient(
            timeout=self._timeout,
            headers={
                "accept": "application/json",
                "Content-Type": "application/json",
                "X-API-Key": self._api_key,
            },
        )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ── Internal ──────────────────────────────────────────────────────────

    def _ensure_open(self) -> httpx.AsyncClient:
        if self._http is None:
            raise GDNError("GDNClient must be used as an async context manager.")
        return self._http

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
    ) -> Any:
        """Execute an HTTP request against the GDN API.

        Args:
            method: HTTP method (GET, POST, …).
            path: Path relative to the API base URL (leading slash optional).
            params: URL query parameters.
            json: JSON request body.

        Returns:
            Parsed JSON response.

        Raises:
            GDNError: On HTTP error or non-200 status.
        """
        http = self._ensure_open()
        url = f"{self._base_url}/{path.lstrip('/')}"
        log.debug("GDNClient %s %s", method, url)

        try:
            response = await http.request(method, url, params=params, json=json)
        except httpx.TimeoutException as exc:
            raise GDNError(f"Request timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise GDNError(f"Network error: {exc}") from exc

        if response.status_code == 401:
            raise GDNError("GDN API authentication failed — check api_key.", status_code=401)
        if response.status_code == 403:
            raise GDNError("GDN API access forbidden — insufficient privileges.", status_code=403)
        if response.status_code == 404:
            raise GDNError(
                f"GDN API endpoint not found: {url}", status_code=404
            )
        if not response.is_success:
            raise GDNError(
                f"GDN API error {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )

        try:
            return response.json()
        except Exception as exc:
            raise GDNError(f"Failed to parse GDN API response as JSON: {exc}") from exc

    # ── Public API ────────────────────────────────────────────────────────

    async def get_usage_summary(self, orgid: str) -> dict:
        """Return total event and user counts for the organisation.

        Calls ``GET /usage_summary/{orgid}``.

        Returns:
            ``{"events": int, "users": int}``
        """
        result = await self._request("GET", f"usage_summary/{orgid}")
        return result if isinstance(result, dict) else {"events": 0, "users": 0}

    async def get_usage_by_authgroup(self, orgid: str) -> list:
        """Return event and user counts broken down by auth group.

        Calls ``GET /usage/{orgid}``.

        Returns:
            List of ``[org_id, authgroup_id, authgroup_name, users, events]``.
        """
        result = await self._request("GET", f"usage/{orgid}")
        return result if isinstance(result, list) else []

    async def get_dashboard_object_data(
        self,
        orgid: str,
        objid: str,
        filters: Optional[dict] = None,
    ) -> dict:
        """Query a dashboard object (report) for the given organisation.

        Calls ``POST /{orgid}/dashboard/object/{resolved_objid}`` with an optional
        filter payload.

        Args:
            orgid: GDN organisation ID.
            objid: Friendly name from ``OBJREFS`` or the full API path.
            filters: Optional filter dict produced by :meth:`build_filter`.

        Returns:
            Raw API response dict (structure varies per object type).
        """
        # Resolve friendly name to full API path
        resolved = OBJREFS.get(objid, objid)
        log.debug("GDNClient dashboard object=%s org=%s filters=%s", resolved, orgid, filters)

        payload = {"filter": filters} if filters else {}
        result = await self._request(
            "POST",
            f"{orgid}/dashboard/object/{resolved}",
            json=payload,
        )
        return result if isinstance(result, dict) else {}

    # ── Filter builder ────────────────────────────────────────────────────

    @staticmethod
    def build_filter(
        *,
        days_ago: Optional[int] = None,
        time_begin: Optional[str] = None,
        time_end: Optional[str] = None,
        username: Optional[str] = None,
        client_ip: Optional[str] = None,
        login_failed: Optional[bool] = None,
        country: Optional[str] = None,
        response: Optional[str] = None,
    ) -> dict:
        """Build a GDN API filter dict, omitting None values.

        Time range logic (in priority order):
        1. If ``time_begin`` and/or ``time_end`` are provided, use them directly.
        2. Else if ``days_ago`` is set, compute ``time_begin = now - days_ago``,
           ``time_end = now``.
        3. Else no time filter is applied.

        Args:
            days_ago: Number of days back from now to set as ``time_begin``.
            time_begin: Explicit start datetime (ISO 8601: ``YYYY-MM-DDTHH:MM``).
            time_end: Explicit end datetime (ISO 8601: ``YYYY-MM-DDTHH:MM``).
            username: Filter by exact or partial username.
            client_ip: Filter by client IP address.
            login_failed: ``True`` = failed logins only; ``False`` = successful only.
            country: Filter by country name.
            response: Filter by treatment response
                (``"accepted"``, ``"hard_notify"``, ``"soft_notify"``, ``"blocked"``).

        Returns:
            Filter dict with None values removed.
        """
        from datetime import datetime, timedelta, timezone

        computed_begin: Optional[str] = time_begin
        computed_end: Optional[str] = time_end

        if not computed_begin and not computed_end and days_ago is not None:
            now = datetime.now(timezone.utc)
            computed_end = now.strftime("%Y-%m-%dT%H:%M")
            computed_begin = (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M")

        raw: dict[str, Any] = {
            "time_begin": computed_begin,
            "time_end": computed_end,
            "username": username,
            "client_ip": client_ip,
            "login_failed": int(login_failed) if login_failed is not None else None,
            "country": country,
            "response": response,
        }
        return {k: v for k, v in raw.items() if v is not None}
