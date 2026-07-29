"""gSage AI — MISP async client wrapper.

Thin async facade over the **synchronous** ``pymisp`` library. Every public
coroutine wraps the blocking call in :func:`asyncio.to_thread`, so the tool
layer stays async-friendly.

Errors
------
All ``pymisp`` exceptions are wrapped in :class:`MISPError`, preserving
the original error message and a short ``code``.

Authentication
--------------
Uses MISP API key (AuthKey) passed via the ``PyMISP`` constructor.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Any, Optional

# pymisp is loaded lazily so this module can still be imported in
# environments where the dependency hasn't been installed yet. Real calls
# fail with a clear MISSING_DEPENDENCY error.
try:  # pragma: no cover — exercised at runtime
    from pymisp import PyMISP, PyMISPError  # type: ignore[import-not-found,unused-ignore]

    _PYMISP_IMPORT_ERROR: Optional[Exception] = None
except Exception as _exc:  # pragma: no cover — import-time fallback
    PyMISP = None  # type: ignore[assignment,misc]
    PyMISPError = Exception  # type: ignore[assignment,misc]
    _PYMISP_IMPORT_ERROR = _exc


log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60.0


class MISPError(Exception):
    """Raised when the MISP API returns an error or connection fails.

    Attributes
    ----------
    status_code : int
        HTTP status code if available, 0 otherwise.
    code : str
        Short error code (e.g. ``"AUTH_FAILED"``, ``"NOT_FOUND"``).
    message : str
        Human-readable error description.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        code: str = "MISP_ERROR",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _classify_pymisp_error(exc: Exception) -> tuple[str, int]:
    """Classify a PyMISP exception into a code and status_code."""
    msg = str(exc).lower()
    if any(w in msg for w in ("401", "unauthorized", "authentication failed", "authkey")):
        return ("AUTH_FAILED", 401)
    if any(w in msg for w in ("403", "forbidden", "permission denied", "denied")):
        return ("PERMISSION_DENIED", 403)
    if any(w in msg for w in ("404", "not found")):
        return ("NOT_FOUND", 404)
    if any(w in msg for w in ("429", "too many requests", "rate limit")):
        return ("RATE_LIMITED", 429)
    if any(w in msg for w in ("500", "502", "503", "504", "server error", "service unavailable")):
        return ("UPSTREAM_ERROR", 500)
    if any(w in msg for w in ("timeout", "timed out", "connection")):
        return ("UPSTREAM_ERROR", 0)
    return ("MISP_ERROR", 0)


class MISPClient:
    """Async wrapper over a synchronous :class:`pymisp.PyMISP` session.

    Parameters
    ----------
    url :
        Base URL of the MISP instance.
    api_key :
        MISP API authentication key.
    verify_cert :
        Verify TLS certificate (default: True).
    timeout :
        HTTP request timeout in seconds (default: 60).
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        verify_cert: bool = True,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        if _PYMISP_IMPORT_ERROR is not None:
            raise MISPError(
                "PyMISP is not installed. Install with: pip install pymisp",
                code="MISSING_DEPENDENCY",
            )
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._verify_cert = verify_cert
        self._timeout = timeout

        try:
            self._sync = PyMISP(url, api_key, ssl=verify_cert, timeout=int(timeout))  # type: ignore[misc]
        except Exception as exc:
            raise MISPError(
                f"Failed to initialise MISP client: {exc}",
                code="CONFIG_ERROR",
            ) from exc

    async def search(
        self,
        controller: str,
        *,
        return_format: str = "json",
        **kwargs: Any,
    ) -> dict | list:
        """Generic async search wrapper.

        Parameters
        ----------
        controller :
            MISP controller to query (``"events"``, ``"attributes"``, etc.).
        return_format :
            Response format (``"json"`` default).
        **kwargs :
            Additional search parameters passed to PyMISP.
        """
        try:
            result = await asyncio.to_thread(
                self._sync.search,
                controller,
                return_format=return_format,
                **kwargs,
            )
            return result
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    async def get_event(self, event_id: str | int) -> dict:
        """Get a single event by ID or UUID."""
        try:
            result = await asyncio.to_thread(
                self._sync.get_event, str(event_id)
            )
            return result
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    async def add_event(self, event: dict) -> dict:
        """Create a new MISP event."""
        try:
            result = await asyncio.to_thread(
                self._sync.add_event, event
            )
            return result
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    async def update_event(self, event_id: str | int, event: dict) -> dict:
        """Update an existing MISP event."""
        try:
            result = await asyncio.to_thread(
                self._sync.update_event, str(event_id), event
            )
            return result
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    async def delete_event(self, event_id: str | int) -> dict:
        """Delete a MISP event."""
        try:
            result = await asyncio.to_thread(
                self._sync.delete_event, str(event_id)
            )
            return result
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    async def publish(self, event_id: str | int, alert: bool = False) -> dict:
        """Publish a MISP event."""
        try:
            result = await asyncio.to_thread(
                self._sync.publish, str(event_id), alert=alert
            )
            return result
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    async def unpublish(self, event_id: str | int) -> dict:
        """Unpublish a MISP event."""
        try:
            result = await asyncio.to_thread(
                self._sync.unpublish, str(event_id)
            )
            return result
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    async def add_attribute(
        self, event_id: str | int, attribute: dict
    ) -> dict:
        """Add an attribute to an event."""
        try:
            result = await asyncio.to_thread(
                self._sync.add_attribute, str(event_id), attribute
            )
            return result
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    async def delete_attribute(self, attribute_id: str | int) -> dict:
        """Delete an attribute."""
        try:
            result = await asyncio.to_thread(
                self._sync.delete_attribute, str(attribute_id)
            )
            return result
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    async def tag(
        self,
        target_uuid: str,
        tag_name: str,
        *,
        local: bool = False,
    ) -> dict:
        """Add a tag to an event or attribute by UUID."""
        try:
            result = await asyncio.to_thread(
                self._sync.tag,  # type: ignore[union-attr]
                str(target_uuid),
                tag_name,
                local=local,
            )
            return result  # type: ignore[return-value]
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    async def untag(
        self,
        target_uuid: str,
        tag_name: str,
    ) -> dict:
        """Remove a tag from an event or attribute by UUID."""
        try:
            result = await asyncio.to_thread(
                self._sync.untag,  # type: ignore[union-attr]
                str(target_uuid),
                tag_name,
            )
            return result  # type: ignore[return-value]
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    async def direct_call(
        self, url_path: str, payload: dict | None = None
    ) -> dict:
        """Make a direct REST call to the MISP API."""
        try:
            result = await asyncio.to_thread(
                self._sync.direct_call,  # type: ignore[union-attr]
                url_path,
                payload,
            )
            return result  # type: ignore[return-value]
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    async def get_version(self) -> dict:
        """Get MISP instance version information via direct call."""
        try:
            result = await asyncio.to_thread(
                self._sync.get_version  # type: ignore[union-attr]
            )
            return result  # type: ignore[return-value]
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    # ── Tags ────────────────────────────────────────────────────────

    async def get_tags_list(self) -> list:
        """List all MISP tags."""
        try:
            result = await asyncio.to_thread(self._sync.tags)
            return result  # type: ignore[return-value]
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    # ── Galaxies ────────────────────────────────────────────────────

    async def get_galaxies_list(self) -> list:
        """List all MISP galaxies."""
        try:
            result = await asyncio.to_thread(self._sync.galaxies)
            return result  # type: ignore[return-value]
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    # ── Organisations ───────────────────────────────────────────────

    async def get_organisations_list(self) -> list:
        """List all MISP organisations."""
        try:
            result = await asyncio.to_thread(self._sync.organisations)
            return result  # type: ignore[return-value]
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    # ── Taxonomies ──────────────────────────────────────────────────

    async def get_taxonomies_list(self) -> list:
        """List all MISP taxonomies."""
        try:
            result = await asyncio.to_thread(self._sync.taxonomies)
            return result  # type: ignore[return-value]
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    # ── Warninglists ────────────────────────────────────────────────

    async def get_warninglists(self) -> list:
        """List all MISP warninglists."""
        try:
            result = await asyncio.to_thread(self._sync.warninglists)
            return result  # type: ignore[return-value]
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc

    # ── Feeds ───────────────────────────────────────────────────────

    async def get_feeds_list(self) -> list:
        """List all MISP feeds."""
        try:
            result = await asyncio.to_thread(self._sync.feeds)
            return result  # type: ignore[return-value]
        except PyMISPError as exc:  # type: ignore[misc]
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
        except Exception as exc:
            code, status = _classify_pymisp_error(exc)
            raise MISPError(str(exc), status_code=status, code=code) from exc
