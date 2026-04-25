"""SEI-PEN REST API v2 async client.

Authentication
--------------
Every API call requires a Bearer token obtained from ``POST /autenticar``.
Tokens are cached in-memory per ``(base_url, usuario, orgao_id)`` tuple with a
30-minute TTL.  A 401 response automatically evicts the cached token, obtains a
new one, and retries the request exactly once.

Environment / base URL mapping
-------------------------------
Production environments follow the URL pattern defined by pengovbr/mod-wssei:

    http://sei.orgao{N}.tramita.processoeletronico.gov.br/
        sei/modulos/wssei/controlador_ws.php/api/v2

Homologação URL patterns are not officially documented; use the ``base_url``
config override when the standard pattern does not apply.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

# ── Environment → base URL mapping ──────────────────────────────────────────

_SEI_PATH = "/sei/modulos/wssei/controlador_ws.php/api/v2"
_TRAMITA_HOST = "tramita.processoeletronico.gov.br"

AMBIENTE_URLS: dict[str, str] = {
    # Production environments
    **{
        f"producao_orgao{n}": f"http://sei.orgao{n}.{_TRAMITA_HOST}{_SEI_PATH}"
        for n in range(1, 6)
    },
    # Homologação environments — URL pattern is a placeholder; override with
    # ``base_url`` in the tool config if your org uses a different host.
    **{
        f"homologacao_orgao{n}": f"http://sei-hom.orgao{n}.{_TRAMITA_HOST}{_SEI_PATH}"
        for n in range(1, 6)
    },
}

AMBIENTE_ENUM = sorted(AMBIENTE_URLS.keys())

# ── In-memory token cache ────────────────────────────────────────────────────

_TOKEN_TTL_SECONDS: int = 30 * 60  # conservative margin below typical SEI session

_token_cache: dict[tuple[str, str, str], "_TokenEntry"] = {}


@dataclass
class _TokenEntry:
    token: str
    expires_at: float  # time.monotonic() deadline


def _cache_key(base_url: str, usuario: str, orgao_id: str) -> tuple[str, str, str]:
    return (base_url, usuario, orgao_id)


def _get_cached_token(key: tuple[str, str, str]) -> Optional[str]:
    entry = _token_cache.get(key)
    if entry is None:
        return None
    if time.monotonic() >= entry.expires_at:
        del _token_cache[key]
        return None
    return entry.token


def _set_cached_token(key: tuple[str, str, str], token: str) -> None:
    _token_cache[key] = _TokenEntry(
        token=token,
        expires_at=time.monotonic() + _TOKEN_TTL_SECONDS,
    )


def _evict_token(key: tuple[str, str, str]) -> None:
    _token_cache.pop(key, None)


# ── Exception ────────────────────────────────────────────────────────────────


class SeiPenError(Exception):
    """Raised when the SEI-PEN API returns an error or config is invalid.

    Attributes
    ----------
    status_code :
        HTTP status code of the failing response, if available.
    """

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ── Client ───────────────────────────────────────────────────────────────────


def resolve_base_url(ambiente: Optional[str], base_url: Optional[str]) -> str:
    """Return the effective API base URL.

    Priority: explicit ``base_url`` > ``ambiente`` mapping.

    Raises :class:`SeiPenError` if neither is sufficient.
    """
    if base_url:
        return base_url.rstrip("/")
    if ambiente:
        url = AMBIENTE_URLS.get(ambiente)
        if url:
            return url
        raise SeiPenError(
            f"Unknown ambiente '{ambiente}'. "
            f"Valid values: {', '.join(AMBIENTE_ENUM)}. "
            "Use base_url to override with a custom URL."
        )
    raise SeiPenError(
        "Either 'ambiente' or 'base_url' must be set in the tool config."
    )


class SeiPenClient:
    """Async REST client for the SEI WSSEI API v2.

    Parameters
    ----------
    base_url :
        Full API base URL (no trailing slash).
    usuario :
        SEI username.
    senha :
        SEI password.
    orgao_id :
        SEI organ/agency numeric ID as a string; converted to ``int`` for auth.
    unidade_id :
        Optional unit ID sent as ``unidade`` header on every request to
        maintain/switch the session unit context.
    timeout :
        HTTP request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        usuario: str,
        senha: str,
        orgao_id: str,
        unidade_id: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._usuario = usuario
        self._senha = senha
        self._orgao_id = orgao_id
        self._unidade_id = unidade_id
        self._timeout = timeout
        self._cache_key = _cache_key(self._base_url, usuario, orgao_id)

    async def _authenticate(self) -> str:
        """Obtain a new token from ``POST /autenticar`` and cache it."""
        auth_url = f"{self._base_url}/autenticar"
        try:
            orgao_int = int(self._orgao_id)
        except ValueError as exc:
            raise SeiPenError(
                f"orgao_id must be a numeric string, got: {self._orgao_id!r}"
            ) from exc

        payload: dict[str, Any] = {
            "usuario": self._usuario,
            "senha": self._senha,
            "orgao": orgao_int,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=True
            ) as client:
                resp = await client.post(auth_url, data=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SeiPenError(
                f"SEI-PEN auth failed: HTTP {exc.response.status_code} — "
                f"{exc.response.text[:300]}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            raise SeiPenError(f"SEI-PEN auth request error: {exc}") from exc

        try:
            body: dict[str, Any] = resp.json()
        except Exception as exc:
            raise SeiPenError(
                f"SEI-PEN: non-JSON auth response: {resp.text[:200]}"
            ) from exc

        if not body.get("sucesso"):
            msg = body.get("mensagem") or body.get("message") or str(body)[:300]
            raise SeiPenError(f"SEI-PEN auth failed: {msg}")

        token: Optional[str] = body.get("data", {}).get("token")
        if not token:
            raise SeiPenError("SEI-PEN: auth succeeded but response contains no token")

        _set_cached_token(self._cache_key, token)
        log.debug(
            "SEI-PEN: authenticated user=%s orgao=%s", self._usuario, self._orgao_id
        )
        return token

    async def _get_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid token, re-authenticating if necessary."""
        if not force_refresh:
            cached = _get_cached_token(self._cache_key)
            if cached:
                return cached
        return await self._authenticate()

    def _build_headers(self, token: str, unidade_override: Optional[str]) -> dict[str, str]:
        headers: dict[str, str] = {"token": token}
        unit = unidade_override or self._unidade_id
        if unit:
            headers["unidade"] = unit
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        unidade_override: Optional[str] = None,
    ) -> dict[str, Any]:
        """Execute an authenticated API request with automatic token refresh.

        On a 401 response the token cache is evicted, a fresh token is obtained,
        and the request is retried exactly once.

        Parameters
        ----------
        method :
            HTTP method: ``"GET"`` or ``"POST"``.
        path :
            API path starting with ``/``, e.g. ``/orgao/listar``.
        params :
            Query string parameters (GET requests).
        data :
            Form-encoded body parameters (POST requests).
        unidade_override :
            Optional unit ID to override the default for this specific request.

        Returns
        -------
        dict
            Parsed JSON response body (the full envelope including ``sucesso``
            and ``data`` fields).

        Raises
        ------
        SeiPenError
            On auth failure, HTTP error, or API-level error (``sucesso: false``).
        """
        url = f"{self._base_url}{path}"
        token = await self._get_token()

        for attempt in range(2):
            headers = self._build_headers(token, unidade_override)
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout, follow_redirects=True
                ) as client:
                    resp = await client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        data=data,
                    )
            except httpx.RequestError as exc:
                raise SeiPenError(f"SEI-PEN request error: {exc}") from exc

            if resp.status_code == 401 and attempt == 0:
                log.warning(
                    "SEI-PEN: 401 on %s %s — evicting token and retrying", method, path
                )
                _evict_token(self._cache_key)
                token = await self._get_token(force_refresh=True)
                continue

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise SeiPenError(
                    f"SEI-PEN HTTP {exc.response.status_code} for {method} {path}: "
                    f"{exc.response.text[:500]}",
                    status_code=exc.response.status_code,
                ) from exc

            try:
                body: dict[str, Any] = resp.json()
            except Exception as exc:
                raise SeiPenError(
                    f"SEI-PEN: non-JSON response for {method} {path}: {resp.text[:300]}"
                ) from exc

            if not body.get("sucesso", True):
                msg = body.get("mensagem") or body.get("message") or str(body)[:400]
                raise SeiPenError(f"SEI-PEN API error: {msg}")

            return body

        # Unreachable — satisfies type checker
        raise SeiPenError("SEI-PEN: max retries exceeded")  # pragma: no cover
