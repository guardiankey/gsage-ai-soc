"""gSage AI — Request Tracker (RT) REST 2.0 async client.

Thin wrapper over ``rt.rest2.AsyncRt`` (https://python-rt.readthedocs.io/) that:

- Normalises configuration loading (URL, token, verify_ssl, timeout, proxy).
- Maps ``python-rt`` exceptions to a single ``RTError`` with a stable code.
- Provides an async context manager for clean resource lifecycle.
- Exposes the subset of operations used by the RT tools (search, get,
  create, edit, comment, reply, take/untake/steal, merge, links, history,
  attachments, queues, users).

Authentication: token only (RT REST 2.0 personal auth token from
"Settings → Auth Tokens" in the RT web UI).
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, AsyncIterator, Optional

import httpx

try:
    # rt>=3 ships rt.rest2 with AsyncRt + typed exceptions
    from rt.rest2 import AsyncRt  # type: ignore[import-untyped]
    from rt.exceptions import (  # type: ignore[import-untyped]
        BadRequestError,
        InvalidUseError,
        NotFoundError,
        UnexpectedMessageFormatError,
        UnexpectedResponseError,
    )
except ImportError:  # pragma: no cover — surfaced at first execute()
    AsyncRt = None  # type: ignore[assignment,misc]
    BadRequestError = InvalidUseError = NotFoundError = (  # type: ignore[assignment,misc]
        UnexpectedMessageFormatError
    ) = UnexpectedResponseError = Exception

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0


def _mask_token(token: str) -> str:
    """Return a masked preview of an auth token for safe logging."""
    if not token:
        return "<empty>"
    if len(token) <= 10:
        return f"{token[:2]}***({len(token)} chars)"
    return f"{token[:4]}…{token[-4:]}({len(token)} chars)"


def _normalise_rt_url(url: str) -> str:
    """Ensure the RT base URL ends with ``/REST/2.0``.

    Accepts any of these forms and normalises to the canonical path:

    * ``https://rt.example.com``             → ``https://rt.example.com/REST/2.0``
    * ``https://rt.example.com/``            → ``https://rt.example.com/REST/2.0``
    * ``https://rt.example.com/REST/2.0``    → unchanged
    * ``https://rt.example.com/REST/2.0/``   → trailing slash stripped
    """
    stripped = url.rstrip("/")
    if not stripped:
        return stripped
    if not stripped.lower().endswith("/rest/2.0"):
        stripped = stripped.rstrip("/") + "/REST/2.0"
    return stripped


class RTError(Exception):
    """Raised when the RT API or transport layer returns an error.

    Attributes
    ----------
    code:
        Stable, agent-friendly error code (e.g. ``"NOT_FOUND"``,
        ``"INVALID_PARAMS"``, ``"AUTH_FAILED"``, ``"CONNECTION_ERROR"``,
        ``"RT_ERROR"``, ``"CONFIG_MISSING"``).
    status_code:
        HTTP status code when known (0 for transport / config errors).
    """

    def __init__(self, message: str, code: str = "RT_ERROR", status_code: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class RTClient:
    """Async Request Tracker REST 2.0 client.

    Parameters
    ----------
    url:
        Base URL of the RT REST 2.0 endpoint, e.g.
        ``https://rt.example.com/REST/2.0``.
    token:
        Personal auth token (sensitive).
    verify_ssl:
        Verify the TLS certificate (default ``True``).
    timeout:
        HTTP request timeout in seconds.
    proxy:
        Optional proxy URL (``http://user:pass@host:port``).

    Usage
    -----
    ::

        async with RTClient(url=..., token=...) as client:
            ticket = await client.get_ticket(42)
            async for row in client.search_tickets(query="Status='open'"):
                ...
    """

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        verify_ssl: bool = True,
        timeout: float = _DEFAULT_TIMEOUT,
        proxy: Optional[str] = None,
    ) -> None:
        self._url = _normalise_rt_url(url or "")
        self._token = token or ""
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._proxy = proxy or None
        self._rt: Optional[AsyncRt] = None  # type: ignore[valid-type]

        log.info(
            "RTClient init: url=%s, token=%s, verify_ssl=%s, timeout=%s, proxy=%s",
            self._url or "<empty>",
            _mask_token(self._token),
            self._verify_ssl,
            self._timeout,
            "set" if self._proxy else "none",
        )

    # ── Context manager ─────────────────────────────────────────────────

    async def __aenter__(self) -> "RTClient":
        self._ensure_configured()
        if AsyncRt is None:
            raise RTError(
                "python-rt is not installed. Add 'rt>=3,<4' to requirements.",
                code="CONFIG_MISSING",
            )
        # ``rt>=3`` accepts verify_cert (bool|str) + http_timeout (int|None)
        self._rt = AsyncRt(
            url=self._url + ("/" if not self._url.endswith("/") else ""),
            token=self._token,
            verify_cert=self._verify_ssl,
            http_timeout=int(self._timeout) if self._timeout else None,
            proxy=self._proxy,
        )
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        # AsyncRt manages its own httpx.AsyncClient internally; closing is a
        # best-effort op.
        client = getattr(self._rt, "session", None)
        if isinstance(client, httpx.AsyncClient):
            try:
                await client.aclose()
            except Exception:  # pragma: no cover
                pass
        self._rt = None

    # ── Internals ───────────────────────────────────────────────────────

    def _ensure_configured(self) -> None:
        if not self._url:
            raise RTError("RT URL is not configured.", code="CONFIG_MISSING")
        if not self._token:
            raise RTError("RT auth token is not configured.", code="CONFIG_MISSING")

    def _rt_or_error(self) -> AsyncRt:  # type: ignore[valid-type]
        if self._rt is None:
            raise RTError(
                "RTClient must be used as an async context manager.",
                code="RT_ERROR",
            )
        return self._rt

    # ── Operation wrappers ──────────────────────────────────────────────
    # All wrappers translate python-rt exceptions to RTError. Callers can
    # rely on a single exception type in the tool layer.

    async def get_ticket(self, ticket_id: int) -> dict:
        try:
            return await self._rt_or_error().get_ticket(ticket_id)  # type: ignore[no-any-return]
        except Exception as exc:  # pragma: no cover - thin shim
            raise self._translate(exc) from exc

    async def search_tickets(
        self,
        *,
        query: Optional[str] = None,
        order: Optional[str] = None,
        fields: Optional[str] = None,
        per_page: Optional[int] = None,
    ) -> list[dict]:
        """Run a TicketSQL search and return up to *per_page* rows.

        Returns a list (not a generator) so the caller can attach metadata
        such as ``total_count`` and ``truncated``.
        """
        kwargs: dict[str, Any] = {}
        if query:
            kwargs["raw_query"] = query
        if order:
            kwargs["order"] = order
        if fields:
            kwargs["query_format"] = "l"  # long format with selected fields
            kwargs["fields"] = fields
        rt_obj = self._rt_or_error()
        rows: list[dict] = []
        cap = per_page or 50
        try:
            async for row in rt_obj.search(**kwargs):  # type: ignore[attr-defined]
                rows.append(row)
                if len(rows) >= cap:
                    break
        except Exception as exc:
            raise self._translate(exc) from exc
        return rows

    async def search_tickets_iter(
        self,
        *,
        query: str,
        fields: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """Stream all matching tickets for a TicketSQL query.

        Used by dashboards that need to walk a bounded result set.
        """
        kwargs: dict[str, Any] = {"raw_query": query}
        if fields:
            kwargs["query_format"] = "l"
            kwargs["fields"] = fields
        rt_obj = self._rt_or_error()
        try:
            async for row in rt_obj.search(**kwargs):  # type: ignore[attr-defined]
                yield row
        except Exception as exc:
            raise self._translate(exc) from exc

    async def create_ticket(
        self,
        *,
        queue: str,
        subject: str,
        content: str,
        content_type: str = "text/plain",
        attachments: Optional[list] = None,
        **kwargs: Any,
    ) -> int:
        try:
            return await self._rt_or_error().create_ticket(  # type: ignore[no-any-return]
                queue=queue,
                subject=subject,
                content=content,
                content_type=content_type,
                attachments=attachments,
                **kwargs,
            )
        except Exception as exc:
            raise self._translate(exc) from exc

    async def edit_ticket(self, ticket_id: int, **kwargs: Any) -> bool:
        try:
            return await self._rt_or_error().edit_ticket(ticket_id, **kwargs)  # type: ignore[no-any-return]
        except Exception as exc:
            raise self._translate(exc) from exc

    async def comment(
        self,
        ticket_id: int,
        *,
        content: str,
        content_type: str = "text/plain",
        attachments: Optional[list] = None,
    ) -> bool:
        try:
            return await self._rt_or_error().comment(  # type: ignore[no-any-return]
                ticket_id,
                content=content,
                content_type=content_type,
                attachments=attachments,
            )
        except Exception as exc:
            raise self._translate(exc) from exc

    async def reply(
        self,
        ticket_id: int,
        *,
        content: str,
        content_type: str = "text/plain",
        attachments: Optional[list] = None,
    ) -> bool:
        try:
            return await self._rt_or_error().reply(  # type: ignore[no-any-return]
                ticket_id,
                content=content,
                content_type=content_type,
                attachments=attachments,
            )
        except Exception as exc:
            raise self._translate(exc) from exc

    async def take(self, ticket_id: int) -> bool:
        try:
            return await self._rt_or_error().take(ticket_id)  # type: ignore[no-any-return]
        except Exception as exc:
            raise self._translate(exc) from exc

    async def untake(self, ticket_id: int) -> bool:
        try:
            return await self._rt_or_error().untake(ticket_id)  # type: ignore[no-any-return]
        except Exception as exc:
            raise self._translate(exc) from exc

    async def steal(self, ticket_id: int) -> bool:
        try:
            return await self._rt_or_error().steal(ticket_id)  # type: ignore[no-any-return]
        except Exception as exc:
            raise self._translate(exc) from exc

    async def merge_ticket(self, ticket_id: int, into_id: int) -> bool:
        try:
            return await self._rt_or_error().merge_ticket(ticket_id, into_id)  # type: ignore[no-any-return]
        except Exception as exc:
            raise self._translate(exc) from exc

    async def get_links(self, ticket_id: int) -> list[dict]:
        try:
            return await self._rt_or_error().get_links(ticket_id)  # type: ignore[no-any-return]
        except Exception as exc:
            raise self._translate(exc) from exc

    async def edit_link(
        self,
        ticket_id: int,
        link_name: str,
        link_value: str,
        delete: bool = False,
    ) -> bool:
        try:
            return await self._rt_or_error().edit_link(  # type: ignore[no-any-return]
                ticket_id, link_name, link_value, delete=delete
            )
        except Exception as exc:
            raise self._translate(exc) from exc

    async def get_ticket_history(self, ticket_id: int) -> list[dict]:
        rt_obj = self._rt_or_error()
        rows: list[dict] = []
        try:
            async for row in rt_obj.get_ticket_history(ticket_id):  # type: ignore[attr-defined]
                rows.append(row)
        except Exception as exc:
            raise self._translate(exc) from exc
        return rows

    async def get_attachments(self, ticket_id: int) -> list[dict]:
        rt_obj = self._rt_or_error()
        rows: list[dict] = []
        try:
            async for row in rt_obj.get_attachments(ticket_id):  # type: ignore[attr-defined]
                rows.append(row)
        except Exception as exc:
            raise self._translate(exc) from exc
        return rows

    async def get_attachment(self, attachment_id: int) -> dict:
        try:
            return await self._rt_or_error().get_attachment(attachment_id)  # type: ignore[no-any-return]
        except Exception as exc:
            raise self._translate(exc) from exc

    async def get_user(self, user_id: str | int) -> dict:
        try:
            return await self._rt_or_error().get_user(user_id)  # type: ignore[no-any-return]
        except Exception as exc:
            raise self._translate(exc) from exc

    async def get_all_queues(self, include_disabled: bool = False) -> list[dict]:
        rt_obj = self._rt_or_error()
        rows: list[dict] = []
        try:
            async for row in rt_obj.get_all_queues(include_disabled=include_disabled):  # type: ignore[attr-defined]
                rows.append(row)
        except Exception as exc:
            raise self._translate(exc) from exc
        return rows

    async def get_queue(self, queue_id: str | int) -> dict:
        try:
            return await self._rt_or_error().get_queue(queue_id)  # type: ignore[no-any-return]
        except Exception as exc:
            raise self._translate(exc) from exc

    # ── Exception translation ───────────────────────────────────────────

    @staticmethod
    def _translate(exc: BaseException) -> RTError:
        """Map a python-rt or transport exception to an :class:`RTError`."""
        # Already wrapped — propagate
        if isinstance(exc, RTError):
            return exc

        # python-rt typed exceptions
        if isinstance(exc, NotFoundError):
            return RTError(str(exc) or "Resource not found", code="NOT_FOUND", status_code=404)
        if isinstance(exc, BadRequestError):
            return RTError(str(exc) or "Bad request", code="INVALID_PARAMS", status_code=400)
        if isinstance(exc, InvalidUseError):
            return RTError(str(exc) or "Invalid request", code="INVALID_PARAMS", status_code=400)
        if isinstance(exc, (UnexpectedResponseError, UnexpectedMessageFormatError)):
            status = getattr(exc, "status_code", 0) or 0
            # 401 / 403 are surfaced inside UnexpectedResponseError messages
            msg = str(exc)
            if "401" in msg or "Unauthorized" in msg:
                return RTError(msg or "Unauthorized", code="AUTH_FAILED", status_code=401)
            if "403" in msg or "Forbidden" in msg:
                return RTError(msg or "Forbidden", code="AUTH_FAILED", status_code=403)
            return RTError(msg or "Unexpected RT response", code="RT_ERROR", status_code=status)

        # httpx transport
        if isinstance(exc, httpx.TimeoutException):
            return RTError(f"RT request timed out: {exc}", code="CONNECTION_ERROR")
        if isinstance(exc, httpx.ConnectError):
            return RTError(f"Cannot connect to RT: {exc}", code="CONNECTION_ERROR")
        if isinstance(exc, httpx.HTTPError):
            return RTError(f"RT transport error: {exc}", code="CONNECTION_ERROR")

        # Catch-all
        return RTError(str(exc) or exc.__class__.__name__, code="RT_ERROR")


# ── Shared config schema ────────────────────────────────────────────────
# All RT tools share the same namespace and schema.

RT_CONFIG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": (
                "Base URL of the RT REST 2.0 endpoint, e.g. "
                "https://rt.example.com/REST/2.0"
            ),
        },
        "token": {
            "type": "string",
            "description": (
                "RT auth token (Settings → Auth Tokens). Sensitive."
            ),
        },
        "verify_ssl": {
            "type": "boolean",
            "description": "Verify TLS certificate (default true).",
        },
        "timeout": {
            "type": "integer",
            "minimum": 5,
            "maximum": 120,
            "description": "HTTP request timeout in seconds (default 30).",
        },
        "proxy": {
            "type": "string",
            "description": (
                "Optional proxy URL (http://user:pass@host:port). Empty "
                "string disables proxy."
            ),
        },
    },
    "additionalProperties": False,
}

RT_CONFIG_DEFAULTS: dict[str, Any] = {
    "url": "",
    "token": "",
    "verify_ssl": True,
    "timeout": 30,
    "proxy": "",
}


def build_rt_client(config: dict) -> RTClient:
    """Construct an :class:`RTClient` from a tool config dict.

    Coerces ``verify_ssl`` and ``timeout`` to the right types and turns
    an empty ``proxy`` string into ``None``.
    """
    raw_verify = config.get("verify_ssl", True)
    if isinstance(raw_verify, str):
        verify = raw_verify.strip().lower() not in {"0", "false", "no", "off", ""}
    else:
        verify = bool(raw_verify)

    raw_timeout = config.get("timeout") or 30
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        timeout = 30.0

    proxy = config.get("proxy") or None

    return RTClient(
        url=config.get("url") or None,
        token=config.get("token") or None,
        verify_ssl=verify,
        timeout=timeout,
        proxy=proxy,
    )
