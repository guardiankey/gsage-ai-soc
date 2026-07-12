"""gSage AI — Trellix EDR async client.

Thin async wrapper over the Trellix EDR public APIs:

- v2 (``api.manage.trellix.com/edr/v2``): realtime SQL-like searches.
- v1 (``api.soc.<region>.trellix.com/active-response/api/v1``): Active Response
  structured searches and remediation actions.

Authentication
--------------
OAuth2 client-credentials at
``auth.trellix.com/auth/realms/IAM/protocol/openid-connect/token`` with
audience ``mcafee`` and scope set
``mi.user.investigate soc.act.tg soc.hts.c soc.hts.r soc.rts.c soc.rts.r``.

The access token is cached in process memory keyed by
``(token_url, client_id, x_api_key)``.  Cache hits avoid issuing new tokens for
every tool execution; cache misses (or 401 responses) trigger a refresh.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from types import TracebackType
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

_DEFAULT_REGION = "us-east-1"
_DEFAULT_TOKEN_URL = (
    "https://auth.trellix.com/auth/realms/IAM/protocol/openid-connect/token"
)
_DEFAULT_BASE_V2 = "https://api.manage.trellix.com"
_DEFAULT_TIMEOUT = 60.0
_DEFAULT_SCOPE = (
    "mi.user.investigate soc.act.tg soc.hts.c soc.hts.r soc.rts.c soc.rts.r"
)

# Process-wide token cache: cache_key -> (access_token, expires_at_epoch).
# Cleared automatically on 401 responses.
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = asyncio.Lock()


class TrellixEDRError(Exception):
    """Raised when the Trellix EDR API returns an error.

    Attributes
    ----------
    status_code : int
        HTTP status code (0 for connection/parse errors).
    code : str
        Short error code (e.g. ``"AUTH_FAILED"``, ``"HTTP_500"``).
    message : str
        Human-readable error message.
    """

    def __init__(self, message: str, *, status_code: int = 0, code: str = "TRELLIX_ERROR") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _cache_key(token_url: str, client_id: str, x_api_key: str) -> str:
    raw = f"{token_url}|{client_id}|{x_api_key}".encode()
    return hashlib.sha256(raw).hexdigest()


class TrellixEDRClient:
    """Async Trellix EDR API client (OAuth2 + v1/v2 + remediation).

    Parameters
    ----------
    client_id, client_secret, x_api_key :
        OAuth2 client-credentials and the ``x-api-key`` header value
        (issued in the Trellix console).
    region :
        Region tag used to build the v1 base URL
        (``api.soc.<region>.trellix.com``).  Default: ``us-east-1``.
    base_url_v2 :
        Override for the v2 base URL.  Default:
        ``https://api.manage.trellix.com``.
    token_url :
        Override for the OAuth2 token endpoint.
    verify_tls :
        Verify TLS certificates (default: True).
    timeout :
        HTTP request timeout in seconds (default: 60).
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        x_api_key: str,
        region: str = _DEFAULT_REGION,
        base_url_v2: Optional[str] = None,
        token_url: Optional[str] = None,
        verify_tls: bool = True,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        if not client_id or not client_secret or not x_api_key:
            raise TrellixEDRError(
                "Trellix EDR credentials are not configured "
                "(client_id, client_secret, x_api_key required).",
                code="MISSING_CREDENTIALS",
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._x_api_key = x_api_key
        self._region = region or _DEFAULT_REGION
        self._base_v2 = (base_url_v2 or _DEFAULT_BASE_V2).rstrip("/")
        self._base_v1 = f"https://api.soc.{self._region}.trellix.com"
        self._token_url = token_url or _DEFAULT_TOKEN_URL
        self._verify_tls = verify_tls
        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None
        self._access_token: Optional[str] = None

    # ── Context manager ────────────────────────────────────────────────────

    async def __aenter__(self) -> "TrellixEDRClient":
        self._http = httpx.AsyncClient(
            timeout=self._timeout,
            verify=self._verify_tls,
            follow_redirects=False,
        )
        await self._ensure_token()
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
            await self._http.aclose()
            self._http = None

    # ── OAuth2 ─────────────────────────────────────────────────────────────

    async def _ensure_token(self, *, force_refresh: bool = False) -> str:
        key = _cache_key(self._token_url, self._client_id, self._x_api_key)
        now = time.time()

        if not force_refresh:
            cached = _TOKEN_CACHE.get(key)
            if cached and cached[1] > now + 30:
                self._access_token = cached[0]
                return cached[0]

        async with _TOKEN_LOCK:
            cached = _TOKEN_CACHE.get(key)
            if not force_refresh and cached and cached[1] > now + 30:
                self._access_token = cached[0]
                return cached[0]

            if self._http is None:
                self._http = httpx.AsyncClient(
                    timeout=self._timeout,
                    verify=self._verify_tls,
                    follow_redirects=False,
                )

            data = {
                "grant_type": "client_credentials",
                "scope": _DEFAULT_SCOPE,
                "audience": "mcafee",
            }
            try:
                resp = await self._http.post(
                    self._token_url,
                    data=data,
                    auth=(self._client_id, self._client_secret),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.RequestError as exc:
                raise TrellixEDRError(
                    f"Network error contacting Trellix IAM: {exc}",
                    code="NETWORK_ERROR",
                ) from exc

            if not resp.is_success:
                raise TrellixEDRError(
                    f"Trellix OAuth2 failed (HTTP {resp.status_code}): {resp.text[:300]}",
                    status_code=resp.status_code,
                    code="AUTH_FAILED",
                )
            try:
                payload = resp.json()
            except Exception as exc:
                raise TrellixEDRError(
                    f"Invalid JSON from Trellix IAM: {exc}",
                    status_code=resp.status_code,
                    code="AUTH_PARSE_ERROR",
                ) from exc

            token = payload.get("access_token")
            expires_in = int(payload.get("expires_in", 600))
            if not token:
                raise TrellixEDRError(
                    "Trellix IAM did not return an access_token.",
                    code="AUTH_FAILED",
                )
            self._access_token = token
            _TOKEN_CACHE[key] = (token, now + expires_in)
            return token

    def _invalidate_token(self) -> None:
        key = _cache_key(self._token_url, self._client_id, self._x_api_key)
        _TOKEN_CACHE.pop(key, None)
        self._access_token = None

    # ── Internal request helper ────────────────────────────────────────────

    def _headers(self, *, content_type: str = "application/vnd.api+json") -> dict[str, str]:
        if not self._access_token:
            raise TrellixEDRError("Trellix client used before authentication.", code="NO_TOKEN")
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": content_type,
            "x-api-key": self._x_api_key,
        }

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[dict] = None,
        allow_303: bool = False,
        params: Optional[dict[str, str | int]] = None,
    ) -> httpx.Response:
        if self._http is None:
            raise TrellixEDRError("Trellix client is not open.", code="CLIENT_CLOSED")

        for attempt in (1, 2):
            try:
                resp = await self._http.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_body,
                    params=params,
                )
            except httpx.RequestError as exc:
                raise TrellixEDRError(
                    f"Network error calling Trellix ({method} {url}): {exc}",
                    code="NETWORK_ERROR",
                ) from exc

            if resp.status_code == 401 and attempt == 1:
                log.info("trellix_edr: 401 received — refreshing token and retrying")
                self._invalidate_token()
                await self._ensure_token(force_refresh=True)
                continue

            if not resp.is_success and not (allow_303 and resp.status_code == 303):
                raise TrellixEDRError(
                    f"Trellix HTTP {resp.status_code} ({method} {url}): {resp.text[:300]}",
                    status_code=resp.status_code,
                    code=f"HTTP_{resp.status_code}",
                )
            return resp

        # Should never reach here.
        raise TrellixEDRError("Trellix request retry loop exhausted.", code="RETRY_EXHAUSTED")

    # ── v2 search (SQL-like) ───────────────────────────────────────────────

    async def start_search_v2(self, query: str) -> str:
        """Start a realtime v2 search.  Returns the query_id."""
        url = f"{self._base_v2}/edr/v2/searches/realtime"
        body = {
            "data": {
                "type": "realTimeSearches",
                "attributes": {"query": query},
            }
        }
        resp = await self._request("POST", url, json_body=body)
        try:
            return resp.json()["data"]["id"]
        except (KeyError, ValueError) as exc:
            raise TrellixEDRError(
                f"Unexpected v2 search response: {resp.text[:300]}",
                status_code=resp.status_code,
                code="BAD_RESPONSE",
            ) from exc

    async def get_status_v2(self, query_id: str) -> bool:
        """Return True when the v2 search is finished (HTTP 303 redirect)."""
        url = f"{self._base_v2}/edr/v2/searches/queue-jobs/{query_id}"
        resp = await self._request("GET", url, allow_303=True)
        return resp.status_code == 303

    async def get_results_v2(
        self,
        query_id: str,
        *,
        next_url: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str], dict]:
        """Fetch one page of v2 results.

        Returns ``(rows, next_link, meta)``.  ``next_link`` is a relative URL
        suffix returned in ``links.next``; pass it back as ``next_url`` to
        continue paging.  ``meta`` is the ``meta`` block from the first page
        (empty dict on subsequent pages).
        """
        if next_url:
            url = f"{self._base_v2}{next_url}"
        else:
            url = f"{self._base_v2}/edr/v2/searches/realtime/{query_id}/results"
        resp = await self._request("GET", url)
        body = resp.json()
        rows = list(body.get("data", []))
        nl = body.get("links", {}).get("next") if isinstance(body.get("links"), dict) else None
        meta = body.get("meta", {}) if isinstance(body.get("meta"), dict) else {}
        return rows, nl, meta

    # ── v1 search (structured) ─────────────────────────────────────────────

    @staticmethod
    def _strip_rts(query_id: str) -> str:
        return query_id.replace("rts-", "") if query_id else query_id

    async def start_search_v1(self, payload: dict) -> str:
        """Start a v1 Active Response search.  Returns the query_id (with ``rts-`` prefix)."""
        url = f"{self._base_v1}/active-response/api/v1/searches"
        resp = await self._request("POST", url, json_body=payload)
        try:
            qid = str(resp.json()["id"])
        except (KeyError, ValueError) as exc:
            raise TrellixEDRError(
                f"Unexpected v1 search response: {resp.text[:300]}",
                status_code=resp.status_code,
                code="BAD_RESPONSE",
            ) from exc
        if not qid.startswith("rts-"):
            qid = f"rts-{qid}"
        return qid

    async def get_status_v1(self, query_id: str) -> bool:
        """v1 status piggy-backs on the v2 queue-jobs endpoint (same backend)."""
        return await self.get_status_v2(query_id)

    async def get_results_v1(
        self,
        query_id: str,
        *,
        offset: int = 0,
        limit: int = 500,
    ) -> tuple[list[dict], dict]:
        """Fetch one page of v1 results.

        Returns ``(items, meta)``.  ``meta`` contains
        ``{"total_count": int, "total_hosts": int}`` on the first page.
        """
        sid = self._strip_rts(query_id)
        url = (
            f"{self._base_v1}/active-response/api/v1/searches/{sid}/results"
            f"?$offset={offset}&$limit={limit}"
        )
        resp = await self._request("GET", url)
        body = resp.json()
        items = list(body.get("items", []))
        meta = {
            "total_count": int(body.get("totalItems", 0)),
            "total_hosts": int(body.get("subscribedHosts", 0)),
        }
        return items, meta

    # ── v1 remediation ─────────────────────────────────────────────────────

    async def start_remediation(
        self,
        *,
        action: str,
        query_id: str,
        row_ids: list[str],
        action_inputs: Optional[list[dict]] = None,
    ) -> str:
        """Trigger a v1 remediation action against rows of a previous search.

        Returns the reaction_id.
        """
        url = f"{self._base_v1}/remediation/api/v1/actions/search-results-actions"
        sid_int = int(self._strip_rts(query_id))
        body = {
            "action": action,
            "actionInputs": action_inputs or [{}],
            "provider": "AR",
            "searchResultsArguments": {
                "arguments": {},
                "searchId": sid_int,
                "rowsIds": [str(r) for r in row_ids],
            },
        }
        resp = await self._request("POST", url, json_body=body)
        try:
            return str(resp.json()["id"])
        except (KeyError, ValueError) as exc:
            raise TrellixEDRError(
                f"Unexpected remediation response: {resp.text[:300]}",
                status_code=resp.status_code,
                code="BAD_RESPONSE",
            ) from exc

    # ── Alerts (v3) ────────────────────────────────────────────────────────

    async def get_alerts(
        self,
        *,
        page_offset: int = 0,
        page_limit: int = 100,
        from_ms: Optional[int] = None,
        to_ms: Optional[int] = None,
        sort: Optional[str] = None,
        filter_str: Optional[str] = None,
    ) -> dict:
        """Fetch one page of v3 alerts (enriched with HostInfo).

        Returns the full JSON:API response dict.
        """
        url = f"{self._base_v2}/edr/v3/alerts"
        params: dict[str, str | int] = {
            "page[offset]": page_offset,
            "page[limit]": page_limit,
        }
        if from_ms is not None:
            params["from"] = from_ms
        if to_ms is not None:
            params["to"] = to_ms
        if sort:
            params["sort"] = sort
        if filter_str:
            params["filter"] = filter_str

        resp = await self._request("GET", url, params=params)
        return resp.json()

    # ── Threats ────────────────────────────────────────────────────────────

    async def get_threats(
        self,
        *,
        page_offset: int = 0,
        page_limit: int = 100,
        from_ms: Optional[int] = None,
        to_ms: Optional[int] = None,
        sort: Optional[str] = None,
        filter_str: Optional[str] = None,
    ) -> dict:
        """Fetch one page of threats from /edr/v2/threats."""
        url = f"{self._base_v2}/edr/v2/threats"
        params: dict[str, str | int] = {
            "page[offset]": page_offset,
            "page[limit]": page_limit,
        }
        if from_ms is not None:
            params["from"] = from_ms
        if to_ms is not None:
            params["to"] = to_ms
        if sort:
            params["sort"] = sort
        if filter_str:
            params["filter"] = filter_str

        resp = await self._request("GET", url, params=params)
        return resp.json()

    async def get_threat_by_id(self, threat_id: str) -> dict:
        """Fetch a single threat by ID from /edr/v2/threats/{id}."""
        url = f"{self._base_v2}/edr/v2/threats/{threat_id}"
        resp = await self._request("GET", url)
        return resp.json()

    async def get_affected_hosts(
        self,
        threat_id: str,
        *,
        page_offset: int = 0,
        page_limit: int = 100,
    ) -> dict:
        """Fetch affected hosts for a threat from /edr/v2/threats/{id}/affectedhosts."""
        url = f"{self._base_v2}/edr/v2/threats/{threat_id}/affectedhosts"
        params: dict[str, str | int] = {
            "page[offset]": page_offset,
            "page[limit]": page_limit,
        }
        resp = await self._request("GET", url, params=params)
        return resp.json()

    async def get_detections_by_threat(
        self,
        threat_id: str,
        *,
        page_offset: int = 0,
        page_limit: int = 100,
    ) -> dict:
        """Fetch detections for a threat from /edr/v2/threats/{id}/detections."""
        url = f"{self._base_v2}/edr/v2/threats/{threat_id}/detections"
        params: dict[str, str | int] = {
            "page[offset]": page_offset,
            "page[limit]": page_limit,
        }
        resp = await self._request("GET", url, params=params)
        return resp.json()

    # ── Trace activity (search-service) ─────────────────────────────────────

    async def get_trace_activity(
        self,
        *,
        trace_id: str,
        ma_guid: str,
        detection_date_epoch_ms: int,
    ) -> dict:
        """Fetch the full activity timeline for a trace.

        Uses the regional SOC search-service endpoint (same base as v1).
        Returns the main-activity-by-trace-id response with ``items[]``
        containing each event in the trace chain (process creations,
        file modifications, image loads, etc.).
        """
        url = f"{self._base_v1}/search-service/api/v1/traces/main-activity-by-trace-id"
        params: dict[str, str | int] = {
            "detectionDate": detection_date_epoch_ms,
            "maGuid": ma_guid,
            "traceId": trace_id,
        }
        resp = await self._request("GET", url, params=params)
        return resp.json()
