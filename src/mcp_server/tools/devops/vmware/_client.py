"""gSage AI — VMware vCenter async client wrapper (shared by all vcenter_* tools).

Authentication uses a vCenter user/password against the vSphere Web
Services (SOAP) API via **pyVmomi**. pyVmomi is synchronous, so every
blocking call is dispatched to a worker thread with ``asyncio.to_thread``
— mirroring how the rest of the codebase handles blocking I/O.

pyVmomi is imported lazily inside :meth:`VCenterClient.connect` so the
module can be imported (and the tools discovered / their schemas listed)
even on hosts where the dependency is not yet installed. A missing
dependency surfaces as a clean :class:`VCenterError` with code
``CONFIG_MISSING`` instead of an ``ImportError`` at registry-build time.

    # Dependency: pyVmomi  (add to the project's requirements alongside the
    # azure-mgmt-* SDKs). Install: ``pip install pyVmomi``.

Configuration fields:

- ``host``: vCenter Server FQDN or IP.
- ``user``: vCenter username (e.g. ``svc-gsage@vsphere.local``).
- ``password``: password (sensitive).
- ``port``: HTTPS port (default 443).
- ``verify_ssl``: validate the server certificate (default true). Set
  false for lab vCenters with self-signed certs.
- ``datacenter``: optional default datacenter name used to scope some
  inventory walks.
- ``timeout``: per-call connect/read timeout in seconds (5–300, default 60).

Usage::

    async with build_vcenter_client(config) as client:
        clusters = await client.list_objs("ClusterComputeResource")
        vm = await client.find_vm(name="SRV-DC01")

All upstream pyVmomi / connection errors are translated to
:class:`VCenterError` with a stable ``code`` so callers can pattern-match
without importing the pyVmomi fault hierarchy.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from types import TracebackType
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config schema / defaults
# ---------------------------------------------------------------------------

VCENTER_CONFIG_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "description": "vCenter Server FQDN or IP address.",
        },
        "user": {
            "type": "string",
            "description": (
                "vCenter username (e.g. 'svc-gsage@vsphere.local')."
            ),
        },
        "password": {
            "type": "string",
            "description": "vCenter password (sensitive).",
        },
        "port": {
            "type": "integer",
            "minimum": 1,
            "maximum": 65535,
            "description": "vCenter HTTPS port (default 443).",
        },
        "verify_ssl": {
            "type": "boolean",
            "description": (
                "Validate the vCenter TLS certificate (default true). "
                "Set false for self-signed lab certificates."
            ),
        },
        "datacenter": {
            "type": "string",
            "description": (
                "Optional default datacenter name used to scope inventory "
                "walks when not provided in params."
            ),
        },
        "timeout": {
            "type": "integer",
            "minimum": 5,
            "maximum": 300,
            "description": "Per-call timeout in seconds (default 60).",
        },
    },
    "required": ["host", "user", "password"],
    "additionalProperties": False,
}

VCENTER_CONFIG_DEFAULTS: dict = {
    "host": "",
    "user": "",
    "password": "",
    "port": 443,
    "verify_ssl": True,
    "datacenter": "",
    "timeout": 60,
}


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class VCenterError(Exception):
    """Raised for vCenter / pyVmomi / transport errors.

    Attributes
    ----------
    code:
        Stable agent-friendly error code:
        ``AUTH_ERROR`` | ``FORBIDDEN`` | ``NOT_FOUND`` | ``CONFLICT`` |
        ``INVALID_PARAMS`` | ``CONNECTION_ERROR`` | ``TIMEOUT`` |
        ``CONFIG_MISSING`` | ``TASK_FAILED`` | ``VCENTER_ERROR``.
    status_code:
        HTTP-ish status hint; 0 for transport / config / fault errors.
    """

    def __init__(
        self,
        message: str,
        code: str = "VCENTER_ERROR",
        status_code: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _translate(exc: BaseException) -> VCenterError:
    """Map an upstream pyVmomi / transport exception to :class:`VCenterError`.

    pyVmomi is imported lazily, so we match on the exception class name and
    module path rather than importing ``pyVmomi.vim`` here. This keeps the
    translator usable even when the dependency is absent (in which case the
    only error we ever raise is ``CONFIG_MISSING`` from :meth:`connect`).
    """
    if isinstance(exc, VCenterError):
        return exc

    name = type(exc).__name__
    module = type(exc).__module__ or ""
    msg = str(exc) or name

    # pyVmomi faults live under pyVmomi.vim.fault / pyVmomi.vmodl.fault.
    if name in ("InvalidLogin", "InvalidLoginException"):
        return VCenterError(
            f"vCenter authentication failed: {msg}",
            code="AUTH_ERROR",
            status_code=401,
        )
    if name in ("NoPermission", "NotAuthenticated", "NotAuthenticatedException"):
        return VCenterError(
            f"vCenter permission denied: {msg}",
            code="FORBIDDEN",
            status_code=403,
        )
    if name in ("ManagedObjectNotFound", "NotFound"):
        return VCenterError(
            f"vCenter object not found: {msg}",
            code="NOT_FOUND",
            status_code=404,
        )
    if name in ("DuplicateName", "AlreadyExists"):
        return VCenterError(
            f"vCenter name conflict: {msg}", code="CONFLICT", status_code=409
        )
    if name in ("InvalidArgument", "InvalidName", "InvalidState"):
        return VCenterError(
            f"Invalid vCenter argument: {msg}",
            code="INVALID_PARAMS",
            status_code=400,
        )
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or name in (
        "timeout",
        "TimeoutError",
    ):
        return VCenterError(f"vCenter request timed out: {msg}", code="TIMEOUT")
    if isinstance(exc, (ConnectionError, OSError)) or "socket" in module or (
        name in ("ConnectionRefusedError", "gaierror", "SSLError", "SSLCertVerificationError")
    ):
        return VCenterError(
            f"vCenter connection error: {msg}", code="CONNECTION_ERROR"
        )
    # Generic vim.fault.* family — keep the message, mark as vCenter error.
    if "pyVmomi" in module or module.startswith("pyVim"):
        return VCenterError(f"vCenter error: {msg}", code="VCENTER_ERROR")
    return VCenterError(f"Unexpected vCenter error: {msg}", code="VCENTER_ERROR")


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def build_vcenter_client(config: dict) -> "VCenterClient":
    """Build a :class:`VCenterClient` from a tool config dict.

    Use as an async context manager so the session is disconnected
    cleanly::

        async with build_vcenter_client(config) as client:
            ...
    """
    return VCenterClient(config)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class VCenterClient:
    """Async wrapper around a pyVmomi ``ServiceInstance``.

    The blocking SOAP session is created in :meth:`__aenter__` (via
    ``asyncio.to_thread``) and torn down in :meth:`__aexit__`. Inventory
    walks and task waits are likewise dispatched to worker threads.
    """

    def __init__(self, config: dict) -> None:
        self._cfg = dict(config or {})
        self._host = (self._cfg.get("host") or "").strip()
        self._user = (self._cfg.get("user") or "").strip()
        self._password = self._cfg.get("password") or ""
        self._port = int(self._cfg.get("port") or 443)
        self._verify_ssl = bool(self._cfg.get("verify_ssl", True))
        self._timeout = float(self._cfg.get("timeout") or 60)
        self._default_dc = (self._cfg.get("datacenter") or "").strip()
        self._si: Any = None  # ServiceInstance
        self._vim: Any = None  # pyVmomi.vim module
        self._closed = False

    @property
    def host(self) -> str:
        return self._host

    @property
    def default_datacenter(self) -> str:
        return self._default_dc

    @property
    def vim(self) -> Any:
        """The lazily-imported ``pyVmomi.vim`` module (managed types)."""
        if self._vim is None:
            raise VCenterError(
                "VCenterClient must be used as an async context manager.",
                code="VCENTER_ERROR",
            )
        return self._vim

    # ── Context manager ─────────────────────────────────────────────────────

    async def __aenter__(self) -> "VCenterClient":
        if not (self._host and self._user and self._password):
            raise VCenterError(
                "vCenter config missing required fields (host, user, "
                "password).",
                code="CONFIG_MISSING",
            )
        try:
            from pyVim.connect import SmartConnect  # noqa: PLC0415
            from pyVmomi import vim  # noqa: PLC0415
        except ImportError as exc:
            raise VCenterError(
                "pyVmomi is not installed on the MCP server. Install it "
                "with 'pip install pyVmomi' to enable the vcenter_* tools.",
                code="CONFIG_MISSING",
            ) from exc

        self._vim = vim

        if self._verify_ssl:
            ssl_ctx: Optional[ssl.SSLContext] = None  # SmartConnect default
        else:
            ssl_ctx = ssl._create_unverified_context()

        def _connect() -> Any:
            return SmartConnect(
                host=self._host,
                user=self._user,
                pwd=self._password,
                port=self._port,
                sslContext=ssl_ctx,
                connectionPoolTimeout=int(self._timeout),
            )

        try:
            self._si = await asyncio.wait_for(
                asyncio.to_thread(_connect), timeout=self._timeout + 10
            )
        except asyncio.TimeoutError as exc:
            raise VCenterError(
                f"Timed out connecting to vCenter {self._host}.",
                code="TIMEOUT",
            ) from exc
        except Exception as exc:
            raise _translate(exc) from exc
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
        if self._si is not None:
            try:
                from pyVim.connect import Disconnect  # noqa: PLC0415

                await asyncio.to_thread(Disconnect, self._si)
            except Exception:
                log.debug("vcenter: error during Disconnect", exc_info=True)
            self._si = None

    # ── Internal helpers ────────────────────────────────────────────────────

    def _content(self) -> Any:
        if self._si is None:
            raise VCenterError(
                "VCenterClient must be used as an async context manager.",
                code="VCENTER_ERROR",
            )
        return self._si.RetrieveContent()

    async def call(self, fn: Callable[[], Any]) -> Any:
        """Run a blocking pyVmomi callable in a thread, translating errors."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn), timeout=self._timeout
            )
        except asyncio.TimeoutError as exc:
            raise VCenterError(
                "vCenter call timed out.", code="TIMEOUT"
            ) from exc
        except Exception as exc:
            raise _translate(exc) from exc

    def _vimtype(self, type_name: str) -> Any:
        """Resolve a ``vim.<Type>`` managed-type by its short name."""
        t = getattr(self.vim, type_name, None)
        if t is None:
            raise VCenterError(
                f"Unknown vSphere managed type {type_name!r}.",
                code="INVALID_PARAMS",
            )
        return t

    # ── Inventory accessors ──────────────────────────────────────────────────

    async def list_objs(self, type_name: str) -> list[Any]:
        """Return every managed object of ``type_name`` in the inventory.

        Uses a recursive ``ContainerView`` rooted at the root folder. The
        view is destroyed before returning. ``type_name`` is a short
        ``vim`` type name such as ``"ClusterComputeResource"``,
        ``"HostSystem"``, ``"VirtualMachine"``, ``"Datastore"``,
        ``"Network"``, ``"ResourcePool"``, ``"Datacenter"`` or ``"Folder"``.
        """
        vimtype = self._vimtype(type_name)

        def _collect() -> list[Any]:
            content = self._content()
            view = content.viewManager.CreateContainerView(
                content.rootFolder, [vimtype], True
            )
            try:
                return list(view.view)
            finally:
                try:
                    view.Destroy()
                except Exception:
                    log.debug("vcenter: error destroying view", exc_info=True)

        return await self.call(_collect)

    async def get_obj(self, type_name: str, name: str) -> Any:
        """Return the single managed object of ``type_name`` named ``name``.

        Raises :class:`VCenterError` (``NOT_FOUND``) when no match exists.
        """
        wanted = (name or "").strip()
        if not wanted:
            raise VCenterError(
                f"A {type_name} name is required.", code="INVALID_PARAMS"
            )
        objs = await self.list_objs(type_name)
        for o in objs:
            try:
                if o.name == wanted:
                    return o
            except Exception:
                continue
        raise VCenterError(
            f"{type_name} named {wanted!r} not found.", code="NOT_FOUND"
        )

    async def find_vm(
        self,
        *,
        name: Optional[str] = None,
        ip: Optional[str] = None,
        uuid: Optional[str] = None,
    ) -> Any:
        """Locate a single VM by ``ip``, ``uuid`` or ``name``.

        ``ip`` and ``uuid`` use the fast ``searchIndex`` lookups; ``name``
        falls back to a recursive inventory walk (DNS-name search would
        miss VMs whose guest name differs from the inventory name). Raises
        :class:`VCenterError` (``NOT_FOUND``) when nothing matches.
        """
        if not (name or ip or uuid):
            raise VCenterError(
                "find_vm requires one of: name, ip, uuid.",
                code="INVALID_PARAMS",
            )

        def _search() -> Any:
            content = self._content()
            si = content.searchIndex
            if ip:
                vm = si.FindByIp(None, ip, True)
                if vm is not None:
                    return vm
            if uuid:
                # instanceUuid=True first, then BIOS uuid fallback.
                vm = si.FindByUuid(None, uuid, True, True)
                if vm is None:
                    vm = si.FindByUuid(None, uuid, True, False)
                if vm is not None:
                    return vm
            if name:
                vm = si.FindByDnsName(None, name, True)
                if vm is not None:
                    return vm
            return None

        vm = await self.call(_search)
        if vm is None and name:
            # Inventory-name fallback (guest DNS name may differ).
            vm = await self.get_obj("VirtualMachine", name)
        if vm is None:
            hint = ip or uuid or name
            raise VCenterError(
                f"No VM matched {hint!r}.", code="NOT_FOUND"
            )
        return vm

    # ── Task handling ────────────────────────────────────────────────────────

    async def wait_for_task(self, task: Any) -> Any:
        """Block until a vCenter ``Task`` finishes; return its ``info.result``.

        Polls ``task.info.state`` in a worker thread. On ``error`` the
        task fault is translated to :class:`VCenterError` (``TASK_FAILED``).
        """

        def _wait() -> Any:
            import time as _time  # noqa: PLC0415

            while True:
                info = task.info
                state = str(info.state)
                if state == "success":
                    return info.result
                if state == "error":
                    fault = getattr(info, "error", None)
                    detail = getattr(fault, "msg", None) or str(fault)
                    raise VCenterError(
                        f"vCenter task failed: {detail}",
                        code="TASK_FAILED",
                    )
                _time.sleep(1.0)

        try:
            return await asyncio.to_thread(_wait)
        except Exception as exc:
            raise _translate(exc) from exc


# Re-export for callers
__all__ = [
    "VCENTER_CONFIG_DEFAULTS",
    "VCENTER_CONFIG_SCHEMA",
    "VCenterClient",
    "VCenterError",
    "build_vcenter_client",
]
