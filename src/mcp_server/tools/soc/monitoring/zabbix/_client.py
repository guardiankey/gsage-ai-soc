"""gSage AI — Zabbix API async client.

Provides a thin async wrapper over ``zabbix_utils.ZabbixAPI`` (which is
synchronous).  Each API call is dispatched via ``asyncio.to_thread`` so
it never blocks the event loop.

Authentication
--------------
1. API token (preferred) — pass ``token=<value>`` in the config row.
2. User/password fallback — pass ``username`` + ``password``.

Usage
-----
::

    async with ZabbixClient(url="https://zabbix.example.com",
                            token="abc123") as client:
        hosts = await client.call("host.get", {"output": ["hostid", "host"]})

The ``ZabbixError`` exception is raised for all API-level, auth, or
network failures.  ``status_code`` is set to the HTTP status code when
available; ``retryable`` is ``True`` for transient (5xx / timeout) errors.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Any, Optional

log = logging.getLogger(__name__)

_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


class ZabbixError(Exception):
    """Raised when the Zabbix API returns an error or the connection fails.

    Attributes
    ----------
    status_code : int
        HTTP status code (0 for connection/parse/auth errors).
    retryable : bool
        Whether the caller can safely retry this error.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ZabbixClient:
    """Async Zabbix API client based on ``zabbix_utils.ZabbixAPI``.

    Parameters
    ----------
    url :
        Base URL of the Zabbix frontend (e.g. ``https://zabbix.example.com``).
    token :
        API token (preferred; mutually exclusive with username/password).
    username :
        Zabbix username for user/password auth.
    password :
        Zabbix password for user/password auth.
    verify_tls :
        Whether to verify the server TLS certificate (default: ``True``).
    timeout :
        HTTP timeout in seconds (default: 30).
    skip_version_check :
        Skip ``zabbix_utils`` API version compatibility check (default:
        ``False``).  Enable for Zabbix versions newer than the library's
        tested range (e.g. Zabbix 8.x).
    """

    def __init__(
        self,
        url: str,
        token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_tls: bool = True,
        timeout: int = 30,
        skip_version_check: bool = False,
    ) -> None:
        if not url:
            raise ZabbixError("Zabbix URL is required.", retryable=False)
        if not token and not (username and password):
            raise ZabbixError(
                "Either 'token' or both 'username' and 'password' must be "
                "provided in the Zabbix tool configuration.",
                retryable=False,
            )
        self._url = self._normalize_url(url)
        self._token = token
        self._username = username
        self._password = password
        self._verify_tls = verify_tls
        self._timeout = timeout
        self._skip_version_check = skip_version_check
        self._api: Any = None  # zabbix_utils.ZabbixAPI, opened on __aenter__

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Strip common accidental suffixes from the Zabbix base URL.

        ``zabbix-utils`` appends ``/api_jsonrpc.php`` automatically.
        If the URL already contains ``api_jsonrpc.php``, or contains PHP
        frontend paths (``zabbix.php``, ``index.php``) or a trailing ``?``,
        the resulting request URL would be malformed.

        Examples
        --------
        - ``https://zabbix.example.com/zabbix.php?``  → ``https://zabbix.example.com``
        - ``https://zabbix.example.com/api_jsonrpc.php`` → same (kept as-is, library accepts it)
        - ``https://zabbix.example.com/zabbix/``      → ``https://zabbix.example.com/zabbix``
        """
        import re as _re
        # Remove everything from the last PHP filename (and optional trailing ?) onwards
        url = _re.sub(r"/(zabbix\.php|index\.php)[?#]?.*$", "", url, flags=_re.IGNORECASE)
        # Strip trailing ? that might remain after other normalisation
        url = url.rstrip("?").rstrip("/")
        return url

    # ── Context manager ────────────────────────────────────────────────────

    async def __aenter__(self) -> "ZabbixClient":
        await self._connect()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Log out and close the underlying synchronous API client."""
        if self._api is not None:
            api = self._api
            self._api = None
            try:
                if self._username and self._password:
                    # Token auth sessions are stateless — no explicit logout needed
                    await asyncio.to_thread(api.user.logout)
            except Exception as exc:  # noqa: BLE001
                log.debug("Zabbix logout error (ignored): %s", exc)

    # ── Internal ───────────────────────────────────────────────────────────

    def _build_api(self) -> Any:
        """Instantiate and authenticate a ``zabbix_utils.ZabbixAPI`` object (sync)."""
        try:
            from zabbix_utils import ZabbixAPI  # type: ignore[import-untyped]
            from zabbix_utils.exceptions import ProcessingError  # type: ignore[import-untyped]  # noqa: F401
        except ImportError as exc:
            raise ZabbixError(
                "zabbix-utils is not installed. "
                "Add 'zabbix-utils>=2.0.2,<3' to requirements.txt and "
                "rebuild the container.",
                retryable=False,
            ) from exc

        try:
            api = ZabbixAPI(
                url=self._url,
                validate_certs=self._verify_tls,
                skip_version_check=self._skip_version_check,
                timeout=self._timeout,
            )
            if self._token:
                api.login(token=self._token)
            else:
                api.login(user=self._username, password=self._password)
            log.debug(
                "Zabbix connected: url=%s version=%s",
                self._url,
                api.api_version(),
            )
            return api
        except Exception as exc:
            msg = str(exc)
            log.error("Zabbix connection error: %s", msg)
            raise ZabbixError(f"Zabbix connection failed: {msg}", retryable=False) from exc

    async def _connect(self) -> None:
        """Open the API connection in a thread pool."""
        self._api = await asyncio.to_thread(self._build_api)

    # ── Public call interface ──────────────────────────────────────────────

    async def call(
        self,
        method: str,
        params: Optional[dict] = None,
    ) -> Any:
        """Invoke a Zabbix API method asynchronously.

        Parameters
        ----------
        method :
            Zabbix API method (e.g. ``"host.get"``).
        params :
            Method parameters dict (optional).

        Returns
        -------
        Any
            The raw result from the Zabbix API.

        Raises
        ------
        ZabbixError
            On API errors, authentication failures, or network issues.
        """
        if self._api is None:
            raise ZabbixError("ZabbixClient is not connected. Use as context manager.", retryable=False)

        service_name, _, method_name = method.partition(".")
        service = getattr(self._api, service_name, None)
        if service is None:
            raise ZabbixError(f"Unknown Zabbix API service: {service_name!r}", retryable=False)

        fn = getattr(service, method_name, None)
        if fn is None:
            raise ZabbixError(
                f"Unknown Zabbix API method: {method!r}", retryable=False
            )

        log.debug("Zabbix call: method=%s params=%s", method, params)
        try:
            result = await asyncio.to_thread(fn, **(params or {}))
        except Exception as exc:
            msg = str(exc)
            # zabbix_utils raises ProcessingError for API-level errors;
            # all other exceptions are treated as network/transport issues
            from zabbix_utils.exceptions import ProcessingError  # type: ignore[import-untyped]

            retryable = not isinstance(exc, ProcessingError)  # type: ignore[misc]
            log.error("Zabbix API error: method=%s error=%s", method, msg)
            raise ZabbixError(
                f"Zabbix API error calling {method!r}: {msg}",
                retryable=retryable,
            ) from exc

        return result
