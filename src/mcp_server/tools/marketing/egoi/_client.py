"""gSage AI — E-goi async client wrapper.

Thin async facade over the **synchronous** ``egoi_api`` SDK (auto-generated
from the OpenAPI v3 spec). Every public coroutine wraps the blocking SDK
call in :func:`asyncio.to_thread`, so the tool layer stays async-friendly.

Response shape
--------------
Every call is invoked with ``skip_deserialization=True``. We then parse the
raw response body with :mod:`json` and return a plain ``dict``/``list``.
This avoids the cost of the openapi-generator Schema instantiation and
keeps the rest of the code dealing with vanilla JSON.

Errors
------
All ``egoi_api.exceptions.ApiException`` instances (and unexpected
exceptions raised by the SDK) are wrapped in :class:`EgoiError`, preserving
``status_code`` (HTTP status), a short ``code`` and a human-readable
``message``.

Authentication
--------------
The E-goi API uses a single ``Apikey`` HTTP header. We pass it via
``Configuration(api_key={'Apikey': <key>}, host=<host>)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import cached_property
from types import TracebackType
from typing import Any, Optional

# egoi_api is loaded lazily so this module can still be imported in
# environments where the dependency isn't installed yet. Real calls fail
# with a clear MISSING_DEPENDENCY error.
try:  # pragma: no cover — exercised at runtime
    import egoi_api  # type: ignore[import-not-found,unused-ignore]
    from egoi_api import ApiClient, Configuration  # type: ignore[import-not-found,unused-ignore]
    from egoi_api.exceptions import ApiException  # type: ignore[import-not-found,unused-ignore]

    _EGOI_IMPORT_ERROR: Optional[Exception] = None
except Exception as _exc:  # pragma: no cover — import-time fallback
    egoi_api = None  # type: ignore[assignment]
    ApiClient = None  # type: ignore[assignment,misc]
    Configuration = None  # type: ignore[assignment,misc]
    ApiException = Exception  # type: ignore[assignment,misc]
    _EGOI_IMPORT_ERROR = _exc


log = logging.getLogger(__name__)


def _install_egoi_serializer_patch() -> None:
    """Patch the egoi_api RFC6570 query-string serializer to accept booleans.

    Background
    ----------
    Endpoints like ``GET /reports/email/{campaign_hash}`` declare bool
    query params (``date``, ``weekday``, ...) which the SDK's input
    validator strictly requires as ``BoolClass``/raw ``bool``. However,
    the SDK's URL builder (``ParameterSerializerBase._ref6570_expansion``)
    only accepts ``type(value) in {str, float, int}`` — using exact type
    identity rather than ``isinstance``, so ``bool`` falls through and
    raises ``Unable to generate a ref6570 representation of True``.

    This catch-22 (validator wants bool / serializer rejects bool) is
    an upstream SDK bug. We patch the serializer once at import time to
    map booleans to the canonical ``"true"``/``"false"`` strings that
    the E-goi API accepts on the wire.

    Idempotent: the patch tags the method with ``_gsage_bool_patch`` so
    repeated imports do not re-wrap it.
    """
    if egoi_api is None:
        return
    try:
        from egoi_api.api_client import ParameterSerializerBase  # type: ignore[import-not-found,unused-ignore]
    except Exception:  # pragma: no cover
        log.warning("egoi_api: ParameterSerializerBase not found — skip bool patch")
        return

    original = ParameterSerializerBase._ref6570_expansion
    if getattr(original, "_gsage_bool_patch", False):
        return

    def _patched(cls, variable_name, in_data, explode, percent_encode, prefix_separator_iterator):  # type: ignore[no-untyped-def]
        # Convert bool to the canonical wire-format string before the
        # original serializer touches it. ``type(in_data) is bool`` matches
        # only literal booleans, not subclasses, mirroring the SDK style.
        if type(in_data) is bool:  # noqa: E721 — exact identity by design
            in_data = "true" if in_data else "false"
        return original.__func__(  # type: ignore[attr-defined]
            cls, variable_name, in_data, explode, percent_encode, prefix_separator_iterator
        )

    _patched._gsage_bool_patch = True  # type: ignore[attr-defined]
    ParameterSerializerBase._ref6570_expansion = classmethod(_patched)  # type: ignore[assignment]


_install_egoi_serializer_patch()


_DEFAULT_HOST = "https://api.egoiapp.com"
_DEFAULT_TIMEOUT = 60.0


class EgoiError(Exception):
    """Raised when the E-goi API call fails.

    Attributes
    ----------
    status_code : int
        HTTP status code returned by the API (0 if no response was received).
    code : str
        Short error code (e.g. ``"EGOI_API_ERROR"``, ``"AUTH_FAILED"``).
    message : str
        Human-readable error message.
    body : Optional[dict | str]
        Decoded error body returned by the API, when available.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        code: str = "EGOI_ERROR",
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.body = body


# ── Helpers ────────────────────────────────────────────────────────────────


def _decode_response(raw_response: Any) -> Any:
    """Return the JSON-decoded body of an SDK ApiResponse, or None.

    The ``skip_deserialization=True`` path returns an :class:`ApiResponse`
    whose ``response`` attribute is the underlying ``urllib3.HTTPResponse``.
    Its ``data`` attribute carries the raw body bytes.
    """
    if raw_response is None:
        return None
    http_resp = getattr(raw_response, "response", None)
    if http_resp is None:
        return None
    data = getattr(http_resp, "data", None)
    if not data:
        return None
    try:
        text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else str(data)
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _parse_error_body(body: Any) -> Optional[Any]:
    """Best-effort decode of the body carried by an ApiException."""
    if body is None:
        return None
    if isinstance(body, (dict, list)):
        return body
    try:
        text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
    except UnicodeDecodeError:
        text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _normalise_query_param_value(value: Any) -> Any:
    """Coerce query params into RFC6570-safe primitives for the SDK.

    The generated E-goi client advertises bool query params, but its
    internal RFC6570 serializer raises on raw ``True``/``False`` values.
    Converting booleans to lowercase strings preserves the wire format the
    API expects while avoiding the SDK bug.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, tuple):
        return [_normalise_query_param_value(item) for item in value]
    if isinstance(value, list):
        return [_normalise_query_param_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalise_query_param_value(item) for key, item in value.items()}
    return value


def _wrap_exception(exc: Exception, *, operation: str) -> EgoiError:
    """Translate an SDK exception into :class:`EgoiError`."""
    if isinstance(exc, ApiException):
        status = int(getattr(exc, "status", 0) or 0)
        reason = str(getattr(exc, "reason", "") or "").strip()
        body = _parse_error_body(getattr(exc, "body", None))
        if status in (401, 403):
            code = "AUTH_FAILED"
        elif status == 404:
            code = "NOT_FOUND"
        elif status == 408:
            code = "TIMEOUT"
        elif status == 413:
            code = "PAYLOAD_TOO_LARGE"
        elif status == 422:
            code = "VALIDATION_ERROR"
        elif status == 429:
            code = "RATE_LIMITED"
        elif 500 <= status < 600:
            code = "UPSTREAM_ERROR"
        else:
            code = "EGOI_API_ERROR"
        # Extract a helpful sub-message from the body when available.
        body_msg = ""
        if isinstance(body, dict):
            body_msg = str(body.get("message") or body.get("detail") or "")[:300]
        msg = f"{operation} failed: HTTP {status} {reason}".strip()
        if body_msg:
            msg = f"{msg} — {body_msg}"
        return EgoiError(msg, status_code=status, code=code, body=body)
    return EgoiError(
        f"{operation} failed: {exc!s}",
        status_code=0,
        code="INTERNAL_ERROR",
    )


class EgoiClient:
    """Async wrapper over a synchronous :mod:`egoi_api` :class:`ApiClient`.

    Parameters
    ----------
    api_key :
        E-goi API key sent in the ``Apikey`` HTTP header.
    host :
        API base URL (default: ``https://api.egoiapp.com``).
    timeout :
        Per-request timeout in seconds (default: 60). Applied as the
        ``timeout`` argument of each SDK call.
    """

    def __init__(
        self,
        *,
        api_key: str,
        host: str = _DEFAULT_HOST,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        if _EGOI_IMPORT_ERROR is not None:
            raise EgoiError(
                f"egoi_api SDK is not installed: {_EGOI_IMPORT_ERROR}. "
                "Add 'egoi-api' (git+https://github.com/e-goi/sdk-python.git) "
                "to requirements.txt and reinstall.",
                code="MISSING_DEPENDENCY",
            )
        if not api_key:
            raise EgoiError(
                "E-goi API key is not configured (api_key required).",
                code="MISSING_CREDENTIALS",
            )
        self._api_key = api_key
        self._host = host or _DEFAULT_HOST
        self._timeout = float(timeout or _DEFAULT_TIMEOUT)
        self._configuration: Optional[Any] = None
        self._api_client: Optional[Any] = None

    # ── Context manager ─────────────────────────────────────────────────

    async def __aenter__(self) -> "EgoiClient":
        # Instantiating the SDK is cheap (no network I/O) so we don't need
        # ``asyncio.to_thread`` here.
        if Configuration is None or ApiClient is None:  # pragma: no cover
            raise EgoiError(
                "egoi_api SDK is not installed.", code="MISSING_DEPENDENCY"
            )
        self._configuration = Configuration(
            host=self._host,
            api_key={"Apikey": self._api_key},
        )
        self._api_client = ApiClient(self._configuration)
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        await self.close()

    async def close(self) -> None:
        client, self._api_client = self._api_client, None
        if client is not None:
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                try:
                    await asyncio.to_thread(close_fn)
                except Exception as exc:  # noqa: BLE001
                    log.warning("egoi: error closing ApiClient: %s", exc)
        # Reset any pooled urllib3 connections at the module level too.
        rest = getattr(self._configuration, "_pool", None) if self._configuration else None
        if rest is not None and hasattr(rest, "clear"):
            try:
                rest.clear()
            except Exception:  # pragma: no cover  # noqa: BLE001
                pass
        self._configuration = None

    def _require_client(self) -> Any:
        if self._api_client is None:
            raise EgoiError(
                "EgoiClient is not connected (use 'async with EgoiClient(...)').",
                code="NOT_CONNECTED",
            )
        return self._api_client

    # ── Lazy API resource accessors ─────────────────────────────────────

    @cached_property
    def _lists_api(self) -> Any:
        from egoi_api.apis.tags.lists_api import ListsApi  # type: ignore[import-not-found]

        return ListsApi(self._require_client())

    @cached_property
    def _contacts_api(self) -> Any:
        from egoi_api.apis.tags.contacts_api import ContactsApi  # type: ignore[import-not-found]

        return ContactsApi(self._require_client())

    @cached_property
    def _campaigns_api(self) -> Any:
        from egoi_api.apis.tags.campaigns_api import CampaignsApi  # type: ignore[import-not-found]

        return CampaignsApi(self._require_client())

    @cached_property
    def _campaign_groups_api(self) -> Any:
        from egoi_api.apis.tags.campaign_groups_api import CampaignGroupsApi  # type: ignore[import-not-found]

        return CampaignGroupsApi(self._require_client())

    @cached_property
    def _reports_api(self) -> Any:
        from egoi_api.apis.tags.reports_api import ReportsApi  # type: ignore[import-not-found]

        return ReportsApi(self._require_client())

    @cached_property
    def _segments_api(self) -> Any:
        from egoi_api.apis.tags.segments_api import SegmentsApi  # type: ignore[import-not-found]

        return SegmentsApi(self._require_client())

    @cached_property
    def _tags_api(self) -> Any:
        from egoi_api.apis.tags.tags_api import TagsApi  # type: ignore[import-not-found]

        return TagsApi(self._require_client())

    # ── Low-level dispatch ──────────────────────────────────────────────

    async def _call(
        self,
        api_instance: Any,
        method_name: str,
        *,
        operation: str,
        query_params: Optional[dict] = None,
        path_params: Optional[dict] = None,
        body: Any = None,
        accept_content_types: Any = None,
        extra_kwargs: Optional[dict] = None,
        coerce_bool_query: bool = True,
    ) -> Any:
        """Invoke ``api_instance.<method_name>(...)`` in a worker thread.

        Every call uses ``skip_deserialization=True`` and returns the
        JSON-decoded body. Errors are wrapped in :class:`EgoiError`.

        ``coerce_bool_query``: when True (default), bool query-param
        values are converted to lowercase strings to work around the
        RFC6570 serializer bug on certain endpoints. Set to False for
        endpoints whose generated schema validates bools strictly
        (``BoolClass``), e.g. ``get_email_report``.
        """
        self._require_client()
        method = getattr(api_instance, method_name, None)
        if method is None:
            raise EgoiError(
                f"egoi_api has no method '{method_name}' on "
                f"{type(api_instance).__name__}.",
                code="UNSUPPORTED_OPERATION",
            )

        kwargs: dict[str, Any] = {
            "skip_deserialization": True,
            "timeout": self._timeout,
        }
        if query_params is not None:
            if coerce_bool_query:
                kwargs["query_params"] = {
                    k: _normalise_query_param_value(v)
                    for k, v in query_params.items()
                    if v is not None
                }
            else:
                kwargs["query_params"] = {
                    k: v for k, v in query_params.items() if v is not None
                }
        if path_params is not None:
            kwargs["path_params"] = path_params
        if body is not None:
            kwargs["body"] = body
        if accept_content_types is not None:
            kwargs["accept_content_types"] = accept_content_types
        if extra_kwargs:
            kwargs.update(extra_kwargs)

        def _run() -> Any:
            try:
                return method(**kwargs)
            except ApiException as exc:
                raise _wrap_exception(exc, operation=operation) from exc
            except Exception as exc:  # noqa: BLE001
                raise _wrap_exception(exc, operation=operation) from exc

        raw = await asyncio.to_thread(_run)
        return _decode_response(raw)

    # ── Lists ───────────────────────────────────────────────────────────

    async def get_all_lists(
        self,
        *,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        order: Optional[str] = None,
        order_by: Optional[str] = None,
    ) -> Any:
        return await self._call(
            self._lists_api,
            "get_all_lists",
            operation="get_all_lists",
            query_params={
                "offset": offset,
                "limit": limit,
                "order": order,
                "order_by": order_by,
            },
        )

    async def get_list(self, list_id: int) -> Any:
        return await self._call(
            self._lists_api,
            "get_list",
            operation="get_list",
            path_params={"list_id": int(list_id)},
        )

    async def create_list(self, body: Any) -> Any:
        return await self._call(
            self._lists_api,
            "create_list",
            operation="create_list",
            body=body,
        )

    async def update_list(self, list_id: int, body: Any) -> Any:
        return await self._call(
            self._lists_api,
            "update_list",
            operation="update_list",
            path_params={"list_id": int(list_id)},
            body=body,
        )

    # ── Contacts ────────────────────────────────────────────────────────

    async def get_all_contacts(
        self,
        list_id: int,
        *,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        order: Optional[str] = None,
        order_by: Optional[str] = None,
        email: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        cellphone: Optional[str] = None,
        telephone: Optional[str] = None,
        language: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Any:
        return await self._call(
            self._contacts_api,
            "get_all_contacts",
            operation="get_all_contacts",
            path_params={"list_id": int(list_id)},
            query_params={
                "offset": offset,
                "limit": limit,
                "order": order,
                "order_by": order_by,
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
                "cellphone": cellphone,
                "telephone": telephone,
                "language": language,
                "status": status,
            },
        )

    async def get_contact(self, list_id: int, contact_id: Any) -> Any:
        # E-goi contact_id is a 10-char hex hash on modern accounts but
        # may also be a numeric id on older lists — forward as-is.
        return await self._call(
            self._contacts_api,
            "get_contact",
            operation="get_contact",
            path_params={
                "list_id": int(list_id),
                "contact_id": str(contact_id),
            },
        )

    async def search_contacts(
        self,
        *,
        contact: str,
        type: str = "email",  # noqa: A002 — matches SDK parameter name
    ) -> Any:
        return await self._call(
            self._contacts_api,
            "search_contacts",
            operation="search_contacts",
            query_params={"contact": contact, "type": type},
        )

    async def get_all_contacts_by_segment(
        self,
        list_id: int,
        segment_id: int,
        *,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> Any:
        return await self._call(
            self._contacts_api,
            "get_all_contacts_by_segment",
            operation="get_all_contacts_by_segment",
            path_params={
                "list_id": int(list_id),
                "segment_id": int(segment_id),
            },
            query_params={"offset": offset, "limit": limit},
        )

    async def create_contact(self, list_id: int, body: Any) -> Any:
        return await self._call(
            self._contacts_api,
            "create_contact",
            operation="create_contact",
            path_params={"list_id": int(list_id)},
            body=body,
        )

    async def patch_contact(self, list_id: int, contact_id: Any, body: Any) -> Any:
        return await self._call(
            self._contacts_api,
            "patch_contact",
            operation="patch_contact",
            path_params={
                "list_id": int(list_id),
                "contact_id": str(contact_id),
            },
            body=body,
        )

    async def action_activate_contacts(self, list_id: int, body: Any) -> Any:
        return await self._call(
            self._contacts_api,
            "action_activate_contacts",
            operation="action_activate_contacts",
            path_params={"list_id": int(list_id)},
            body=body,
        )

    async def action_deactivate_contacts(self, list_id: int, body: Any) -> Any:
        return await self._call(
            self._contacts_api,
            "action_deactivate_contacts",
            operation="action_deactivate_contacts",
            path_params={"list_id": int(list_id)},
            body=body,
        )

    async def action_unsubscribe_contact(self, list_id: int, body: Any) -> Any:
        return await self._call(
            self._contacts_api,
            "action_unsubscribe_contact",
            operation="action_unsubscribe_contact",
            path_params={"list_id": int(list_id)},
            body=body,
        )

    async def action_forget_contacts(self, list_id: int, body: Any) -> Any:
        return await self._call(
            self._contacts_api,
            "action_forget_contacts",
            operation="action_forget_contacts",
            path_params={"list_id": int(list_id)},
            body=body,
        )

    async def action_attach_tag(self, list_id: int, body: Any) -> Any:
        return await self._call(
            self._contacts_api,
            "action_attach_tag",
            operation="action_attach_tag",
            path_params={"list_id": int(list_id)},
            body=body,
        )

    async def action_detach_tag(self, list_id: int, body: Any) -> Any:
        return await self._call(
            self._contacts_api,
            "action_detach_tag",
            operation="action_detach_tag",
            path_params={"list_id": int(list_id)},
            body=body,
        )

    async def action_import_bulk(self, list_id: int, body: Any) -> Any:
        return await self._call(
            self._contacts_api,
            "action_import_bulk",
            operation="action_import_bulk",
            path_params={"list_id": int(list_id)},
            body=body,
        )

    # ── Campaigns ───────────────────────────────────────────────────────

    async def get_all_campaigns(
        self,
        *,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        order: Optional[str] = None,
        order_by: Optional[str] = None,
        type: Optional[str] = None,  # noqa: A002
        status: Optional[str] = None,
        group_id: Optional[int] = None,
    ) -> Any:
        return await self._call(
            self._campaigns_api,
            "get_all_campaigns",
            operation="get_all_campaigns",
            query_params={
                "offset": offset,
                "limit": limit,
                "order": order,
                "order_by": order_by,
                "type": type,
                "status": status,
                "group_id": group_id,
            },
        )

    # ── Campaign groups ─────────────────────────────────────────────────

    async def get_all_campaign_groups(
        self,
        *,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        group_id: Optional[int] = None,
        name: Optional[str] = None,
    ) -> Any:
        return await self._call(
            self._campaign_groups_api,
            "get_all_campaign_groups",
            operation="get_all_campaign_groups",
            query_params={
                "offset": offset,
                "limit": limit,
                "group_id": group_id,
                "name": name,
            },
        )

    # ── Reports ─────────────────────────────────────────────────────────

    async def get_email_report(
        self,
        campaign_hash: str,
        *,
        date: Optional[bool] = None,
        weekday: Optional[bool] = None,
        hour: Optional[bool] = None,
        location: Optional[bool] = None,
        domain: Optional[bool] = None,
        url: Optional[bool] = None,
        reader: Optional[bool] = None,
    ) -> Any:
        """Fetch the email-campaign report.

        Each boolean flag enables a server-side breakdown section. Setting
        them all to ``True`` (or all to ``None``) yields the full report —
        E-goi returns whatever sections the campaign supports.
        """
        return await self._call(
            self._reports_api,
            "get_email_report",
            operation="get_email_report",
            path_params={"campaign_hash": str(campaign_hash)},
            query_params={
                "date": date,
                "weekday": weekday,
                "hour": hour,
                "location": location,
                "domain": domain,
                "url": url,
                "reader": reader,
            },
            # This endpoint's generated schema validates bool params
            # via ``BoolClass`` and rejects the string coercion used
            # elsewhere as an RFC6570 workaround.
            coerce_bool_query=False,
        )

    # ── Segments ────────────────────────────────────────────────────────

    async def get_all_segments(
        self,
        list_id: int,
        *,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> Any:
        return await self._call(
            self._segments_api,
            "get_all_segments",
            operation="get_all_segments",
            path_params={"list_id": int(list_id)},
            query_params={"offset": offset, "limit": limit},
        )

    # ── Tags ────────────────────────────────────────────────────────────

    async def get_all_tags(
        self,
        *,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> Any:
        return await self._call(
            self._tags_api,
            "get_all_tags",
            operation="get_all_tags",
            query_params={"offset": offset, "limit": limit},
        )
