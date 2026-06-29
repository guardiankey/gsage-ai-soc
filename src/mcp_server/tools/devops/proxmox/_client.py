"""gSage AI — Proxmox VE async client wrapper (shared by all proxmox_* tools).

Proxmox VE exposes a clean JSON REST API at ``https://{host}:8006/api2/json``.
Unlike vCenter (SOAP / pyVmomi) this maps directly onto ``httpx.AsyncClient``,
so the client is fully async with no thread offloading.

Authentication uses an **API token** (Proxmox VE 6.2+, standard on 7/8):

    Authorization: PVEAPIToken={token_id}={token_secret}

where ``token_id`` is ``user@realm!tokenname``. This is stateless — no
ticket / CSRF dance — and is the recommended method.

    # Dependency: httpx (already used across the codebase — no new dependency).

Configuration fields:

- ``host``: Proxmox node FQDN or IP (any cluster node; it proxies the rest).
- ``port``: API port (default 8006).
- ``token_id``: API token id ``user@realm!tokenname``.
- ``token_secret``: API token secret/UUID (sensitive).
- ``verify_ssl``: validate the TLS certificate (default true). Set false
  for the default self-signed PVE certificate in labs.
- ``node``: optional default node name used when an action does not carry
  one and it cannot be resolved from the cluster.
- ``timeout``: per-request timeout in seconds (5–300, default 60).

Usage::

    async with build_proxmox_client(config) as client:
        vms = await client.get("/cluster/resources", type="vm")
        node, kind, info = await client.locate_guest(100)

All upstream HTTP / transport errors are translated to
:class:`ProxmoxError` with a stable ``code`` so callers can pattern-match.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config schema / defaults
# ---------------------------------------------------------------------------

# Per-cluster connection fields, shared by the top-level (the implicit
# ``default`` cluster) and by every named entry under ``profiles``.
_PROXMOX_CLUSTER_PROPERTIES: dict = {
    "host": {
        "type": "string",
        "description": "Proxmox node FQDN or IP (any cluster node).",
    },
    "port": {
        "type": "integer",
        "minimum": 1,
        "maximum": 65535,
        "description": "Proxmox API port (default 8006).",
    },
    "token_id": {
        "type": "string",
        "description": (
            "API token id in the form 'user@realm!tokenname' "
            "(e.g. 'svc-gsage@pve!gsage')."
        ),
    },
    "token_secret": {
        "type": "string",
        "description": "API token secret / UUID (sensitive).",
    },
    "verify_ssl": {
        "type": "boolean",
        "description": (
            "Validate the Proxmox TLS certificate (default true). Set "
            "false for the default self-signed PVE certificate."
        ),
    },
    "node": {
        "type": "string",
        "description": (
            "Optional default node name used when an action omits it "
            "and it cannot be resolved from the cluster."
        ),
    },
    "timeout": {
        "type": "integer",
        "minimum": 5,
        "maximum": 300,
        "description": "Per-request timeout in seconds (default 60).",
    },
}

PROXMOX_CONFIG_SCHEMA: dict = {
    "type": "object",
    "properties": {
        # Top-level fields describe the primary cluster, selected when the
        # 'profile' param is omitted or set to 'default'.
        **_PROXMOX_CLUSTER_PROPERTIES,
        # Additional clusters, each selectable by name via the 'profile'
        # param. A named profile is a self-contained cluster config.
        "profiles": {
            "type": "object",
            "description": (
                "Additional named Proxmox clusters. Each key is a profile "
                "name selectable via the 'profile' param; its value accepts "
                "the same fields as the top level. The top-level fields "
                "define the implicit 'default' cluster."
            ),
            "additionalProperties": {
                "type": "object",
                "properties": _PROXMOX_CLUSTER_PROPERTIES,
                "required": ["host", "token_id", "token_secret"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["host", "token_id", "token_secret"],
    "additionalProperties": False,
}

PROXMOX_CONFIG_DEFAULTS: dict = {
    "host": "",
    "port": 8006,
    "token_id": "",
    "token_secret": "",
    "verify_ssl": True,
    "node": "",
    "timeout": 60,
    "profiles": {},
}


def resolve_proxmox_profile(config: dict, profile: Optional[str]) -> dict:
    """Return the flat cluster config for the selected ``profile``.

    ``profile`` empty or ``"default"`` selects the top-level fields (the
    primary cluster). Any other value is looked up under ``profiles``;
    a missing name raises :class:`ProxmoxError` (``CONFIG_MISSING``).
    """
    name = (profile or "").strip()
    if not name or name == "default":
        return dict(config or {})
    profiles = (config or {}).get("profiles") or {}
    cluster = profiles.get(name)
    if not isinstance(cluster, dict):
        available = ", ".join(sorted(profiles.keys())) or "(none)"
        raise ProxmoxError(
            f"Proxmox profile {name!r} is not configured. Available extra "
            f"profiles: {available}. Omit 'profile' (or use 'default') for "
            "the primary cluster.",
            code="CONFIG_MISSING",
        )
    return dict(cluster)


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class ProxmoxError(Exception):
    """Raised for Proxmox API / transport errors.

    Attributes
    ----------
    code:
        Stable agent-friendly error code:
        ``AUTH_ERROR`` | ``FORBIDDEN`` | ``NOT_FOUND`` | ``CONFLICT`` |
        ``INVALID_PARAMS`` | ``RATE_LIMITED`` | ``CONNECTION_ERROR`` |
        ``TIMEOUT`` | ``CONFIG_MISSING`` | ``TASK_FAILED`` |
        ``PROXMOX_ERROR``.
    status_code:
        HTTP status code; 0 for transport / config errors.
    """

    def __init__(
        self,
        message: str,
        code: str = "PROXMOX_ERROR",
        status_code: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


_HTTP_CODE_MAP = {
    400: "INVALID_PARAMS",
    401: "AUTH_ERROR",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    429: "RATE_LIMITED",
}


def _translate(exc: BaseException) -> ProxmoxError:
    """Map an upstream httpx exception to :class:`ProxmoxError`."""
    if isinstance(exc, ProxmoxError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return ProxmoxError(f"Proxmox request timed out: {exc}", code="TIMEOUT")
    if isinstance(exc, httpx.ConnectError):
        return ProxmoxError(
            f"Proxmox connection error: {exc}", code="CONNECTION_ERROR"
        )
    if isinstance(exc, httpx.TransportError):
        return ProxmoxError(
            f"Proxmox transport error: {exc}", code="CONNECTION_ERROR"
        )
    return ProxmoxError(f"Unexpected Proxmox error: {exc}", code="PROXMOX_ERROR")


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def build_proxmox_client(
    config: dict, profile: Optional[str] = None
) -> "ProxmoxClient":
    """Build a :class:`ProxmoxClient` for the selected cluster ``profile``.

    ``profile`` selects which configured cluster to connect to (see
    :func:`resolve_proxmox_profile`); omit it or pass ``"default"`` for the
    primary cluster. Use as an async context manager so the underlying HTTP
    connection pool is closed cleanly::

        async with build_proxmox_client(config, profile="clusterB") as client:
            ...
    """
    return ProxmoxClient(resolve_proxmox_profile(config, profile))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ProxmoxClient:
    """Async wrapper around the Proxmox VE REST API (httpx + API token)."""

    def __init__(self, config: dict) -> None:
        self._cfg = dict(config or {})
        self._host = (self._cfg.get("host") or "").strip()
        self._port = int(self._cfg.get("port") or 8006)
        self._token_id = (self._cfg.get("token_id") or "").strip()
        self._token_secret = (self._cfg.get("token_secret") or "").strip()
        self._verify_ssl = bool(self._cfg.get("verify_ssl", True))
        self._timeout = float(self._cfg.get("timeout") or 60)
        self._default_node = (self._cfg.get("node") or "").strip()
        self._http: Optional[httpx.AsyncClient] = None
        self._closed = False

    @property
    def host(self) -> str:
        return self._host

    @property
    def default_node(self) -> str:
        return self._default_node

    # ── Context manager ─────────────────────────────────────────────────────

    async def __aenter__(self) -> "ProxmoxClient":
        if not (self._host and self._token_id and self._token_secret):
            raise ProxmoxError(
                "Proxmox config missing required fields (host, token_id, "
                "token_secret).",
                code="CONFIG_MISSING",
            )
        base_url = f"https://{self._host}:{self._port}/api2/json"
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": (
                    f"PVEAPIToken={self._token_id}={self._token_secret}"
                )
            },
            verify=self._verify_ssl,
            timeout=self._timeout,
        )
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        if self._closed:
            return
        self._closed = True
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                log.debug("proxmox: error closing http client", exc_info=True)
            self._http = None

    # ── Core request ─────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise ProxmoxError(
                "ProxmoxClient must be used as an async context manager.",
                code="PROXMOX_ERROR",
            )
        return self._http

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
    ) -> Any:
        """Issue a request and return the unwrapped ``data`` payload.

        Proxmox wraps every response as ``{"data": ...}``. On a non-2xx
        status the body (and ``errors`` field, when present) is folded into
        a :class:`ProxmoxError` with a translated ``code``.
        """
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        clean_data = {k: v for k, v in (data or {}).items() if v is not None}
        try:
            resp = await self._client().request(
                method,
                path,
                params=clean_params or None,
                data=clean_data or None,
            )
        except Exception as exc:
            raise _translate(exc) from exc

        if resp.status_code >= 400:
            code = _HTTP_CODE_MAP.get(resp.status_code, "PROXMOX_ERROR")
            detail = self._error_detail(resp)
            raise ProxmoxError(
                f"{detail} (HTTP {resp.status_code})",
                code=code,
                status_code=resp.status_code,
            )
        try:
            body = resp.json()
        except Exception:
            return None
        return body.get("data") if isinstance(body, dict) else body

    @staticmethod
    def _error_detail(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            errors = body.get("errors")
            if errors:
                parts = "; ".join(f"{k}: {v}" for k, v in errors.items())
                return f"Proxmox API error: {parts}"
            if body.get("message"):
                return f"Proxmox API error: {body['message']}"
        except Exception:
            pass
        text = (resp.text or "").strip()
        return f"Proxmox API error: {text[:300]}" if text else "Proxmox API error"

    # ── Convenience verbs ────────────────────────────────────────────────────

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, **data: Any) -> Any:
        return await self.request("POST", path, data=data)

    async def put(self, path: str, **data: Any) -> Any:
        return await self.request("PUT", path, data=data)

    async def delete(self, path: str, **params: Any) -> Any:
        return await self.request("DELETE", path, params=params)

    # ── Guest / node resolution ──────────────────────────────────────────────

    async def cluster_resources(
        self, resource_type: Optional[str] = None
    ) -> list[dict]:
        """Return ``/cluster/resources`` rows, optionally filtered by type."""
        rows = await self.get("/cluster/resources", type=resource_type)
        return list(rows or [])

    async def locate_guest(self, vmid: int) -> tuple[str, str, dict]:
        """Resolve ``(node, kind, resource_row)`` for a VMID across the cluster.

        ``kind`` is ``"qemu"`` or ``"lxc"``. Raises :class:`ProxmoxError`
        (``NOT_FOUND``) when no guest with that VMID exists.
        """
        rows = await self.cluster_resources("vm")
        for r in rows:
            if int(r.get("vmid", -1)) == int(vmid):
                node = str(r.get("node") or "")
                kind = str(r.get("type") or "qemu")
                return node, kind, r
        raise ProxmoxError(
            f"No guest with VMID {vmid} found in the cluster.",
            code="NOT_FOUND",
        )

    async def find_guest_by_name(self, name: str) -> tuple[str, str, dict]:
        """Resolve ``(node, kind, resource_row)`` for a guest by name.

        Raises ``NOT_FOUND`` when no match, ``CONFLICT`` when the name is
        ambiguous (duplicate names are legal in Proxmox).
        """
        wanted = (name or "").strip()
        rows = [
            r for r in await self.cluster_resources("vm")
            if str(r.get("name") or "") == wanted
        ]
        if not rows:
            raise ProxmoxError(
                f"No guest named {wanted!r} found.", code="NOT_FOUND"
            )
        if len(rows) > 1:
            vmids = ", ".join(str(r.get("vmid")) for r in rows)
            raise ProxmoxError(
                f"Guest name {wanted!r} is ambiguous (VMIDs: {vmids}); "
                "use vmid instead.",
                code="CONFLICT",
            )
        r = rows[0]
        return str(r.get("node") or ""), str(r.get("type") or "qemu"), r

    def resolve_node(self, params: Optional[dict]) -> str:
        """Resolve a node name from params or the profile default."""
        node = ""
        if params:
            node = (params.get("node") or "").strip()
        if not node:
            node = self._default_node
        if not node:
            raise ProxmoxError(
                "node is required (set 'node' in the profile or pass 'node' "
                "in params).",
                code="INVALID_PARAMS",
            )
        return node

    # ── Async task (UPID) handling ───────────────────────────────────────────

    @staticmethod
    def is_upid(value: Any) -> bool:
        return isinstance(value, str) and value.startswith("UPID:")

    async def wait_for_task(self, node: str, upid: str) -> dict:
        """Poll a Proxmox task (UPID) until it stops; return its status row.

        On a non-``OK`` ``exitstatus`` the task is considered failed and a
        :class:`ProxmoxError` (``TASK_FAILED``) is raised with the exit
        status text.
        """
        deadline = asyncio.get_event_loop().time() + max(self._timeout * 10, 600)
        path = f"/nodes/{node}/tasks/{upid}/status"
        while True:
            status = await self.get(path)
            if isinstance(status, dict) and status.get("status") == "stopped":
                exit_status = str(status.get("exitstatus") or "")
                if exit_status and not exit_status.startswith("OK"):
                    raise ProxmoxError(
                        f"Proxmox task failed: {exit_status}",
                        code="TASK_FAILED",
                    )
                return status
            if asyncio.get_event_loop().time() > deadline:
                raise ProxmoxError(
                    f"Timed out waiting for Proxmox task {upid}.",
                    code="TIMEOUT",
                )
            await asyncio.sleep(1.5)

    async def run_task(self, node: str, result: Any) -> Optional[dict]:
        """If ``result`` is a UPID, wait for it; otherwise return ``None``.

        Many Proxmox mutations return a UPID string for an async worker
        task; some config edits return ``null``. This normalises both.
        """
        if self.is_upid(result):
            return await self.wait_for_task(node, result)
        return None


# Re-export for callers
__all__ = [
    "PROXMOX_CONFIG_DEFAULTS",
    "PROXMOX_CONFIG_SCHEMA",
    "ProxmoxClient",
    "ProxmoxError",
    "build_proxmox_client",
    "resolve_proxmox_profile",
]
