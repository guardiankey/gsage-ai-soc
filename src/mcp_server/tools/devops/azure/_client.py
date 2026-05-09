"""gSage AI — Azure async client wrapper (shared by all azure_* tools).

Authentication uses an Azure Service Principal (``tenant_id`` /
``client_id`` / ``client_secret``). The official Azure SDK already ships
async variants of every management plane client under the ``.aio``
submodule, so we use them directly — no ``asyncio.to_thread`` is needed.

Configuration fields:

- ``tenant_id``: Microsoft Entra ID (Azure AD) tenant ID.
- ``client_id``: Service Principal application (client) ID.
- ``client_secret``: Service Principal secret (sensitive).
- ``default_subscription_id``: Subscription used when ``params.subscription_id``
  is omitted.
- ``cloud_environment``: ``AzurePublicCloud`` (default), ``AzureUSGovernment``,
  ``AzureChinaCloud``, or ``AzureGermanCloud``.
- ``timeout``: HTTP request timeout (seconds, 5–300, default 60).
- ``default_resource_group``: optional default RG for management actions.

Usage::

    async with build_azure_client(config) as client:
        sub_id = client.resolve_subscription(params)
        async for vm in client.compute(sub_id).virtual_machines.list_all():
            ...

All upstream errors are translated to :class:`AzureError` with a stable
``code`` so callers can pattern-match without depending on the upstream
exception types.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, AsyncIterable, Optional

from azure.core.exceptions import (
    AzureError as _SDKAzureError,
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceRequestTimeoutError,
)
from azure.identity.aio import ClientSecretCredential
from azure.mgmt.advisor.aio import AdvisorManagementClient
from azure.mgmt.compute.aio import ComputeManagementClient
from azure.mgmt.containerservice.aio import ContainerServiceClient
from azure.mgmt.costmanagement.aio import CostManagementClient
from azure.mgmt.devtestlabs.aio import DevTestLabsClient
from azure.mgmt.monitor.aio import MonitorManagementClient
from azure.mgmt.network.aio import NetworkManagementClient
from azure.mgmt.resource.resources.aio import ResourceManagementClient
from azure.mgmt.resource.subscriptions.aio import (
    SubscriptionClient as ResourceSubscriptionClient,
)
from azure.mgmt.sql.aio import SqlManagementClient
from azure.mgmt.storage.aio import StorageManagementClient
from azure.mgmt.subscription.aio import SubscriptionClient
from azure.mgmt.web.aio import WebSiteManagementClient

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cloud environment endpoints
# ---------------------------------------------------------------------------

_CLOUD_ENVIRONMENTS = {
    "AzurePublicCloud": {
        "active_directory": "https://login.microsoftonline.com",
        "resource_manager": "https://management.azure.com",
        "credential_scopes": ("https://management.azure.com/.default",),
    },
    "AzureUSGovernment": {
        "active_directory": "https://login.microsoftonline.us",
        "resource_manager": "https://management.usgovcloudapi.net",
        "credential_scopes": ("https://management.usgovcloudapi.net/.default",),
    },
    "AzureChinaCloud": {
        "active_directory": "https://login.chinacloudapi.cn",
        "resource_manager": "https://management.chinacloudapi.cn",
        "credential_scopes": ("https://management.chinacloudapi.cn/.default",),
    },
    "AzureGermanCloud": {
        "active_directory": "https://login.microsoftonline.de",
        "resource_manager": "https://management.microsoftazure.de",
        "credential_scopes": ("https://management.microsoftazure.de/.default",),
    },
}


# ---------------------------------------------------------------------------
# Config schema / defaults
# ---------------------------------------------------------------------------

AZURE_CONFIG_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tenant_id": {
            "type": "string",
            "description": "Microsoft Entra ID (Azure AD) tenant ID (UUID).",
        },
        "client_id": {
            "type": "string",
            "description": "Service Principal application (client) ID.",
        },
        "client_secret": {
            "type": "string",
            "description": "Service Principal secret (sensitive).",
        },
        "default_subscription_id": {
            "type": "string",
            "description": (
                "Subscription used when the caller omits "
                "'params.subscription_id'."
            ),
        },
        "cloud_environment": {
            "type": "string",
            "enum": sorted(_CLOUD_ENVIRONMENTS.keys()),
            "description": (
                "Azure cloud environment (default 'AzurePublicCloud')."
            ),
        },
        "timeout": {
            "type": "integer",
            "minimum": 5,
            "maximum": 300,
            "description": "HTTP request timeout in seconds (default 60).",
        },
        "default_resource_group": {
            "type": "string",
            "description": (
                "Optional default resource group used by management "
                "actions when not provided in params."
            ),
        },
    },
    "additionalProperties": False,
}

AZURE_CONFIG_DEFAULTS: dict = {
    "tenant_id": "",
    "client_id": "",
    "client_secret": "",
    "default_subscription_id": "",
    "cloud_environment": "AzurePublicCloud",
    "timeout": 60,
    "default_resource_group": "",
}


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class AzureError(Exception):
    """Raised for Azure SDK / transport errors.

    Attributes
    ----------
    code:
        Stable agent-friendly error code:
        ``AUTH_ERROR`` | ``FORBIDDEN`` | ``NOT_FOUND`` | ``CONFLICT`` |
        ``INVALID_PARAMS`` | ``RATE_LIMITED`` | ``UPSTREAM_ERROR`` |
        ``CONNECTION_ERROR`` | ``CONFIG_MISSING`` | ``TIMEOUT`` |
        ``AZURE_ERROR``.
    status_code:
        HTTP status code; 0 for transport / config errors.
    """

    def __init__(
        self,
        message: str,
        code: str = "AZURE_ERROR",
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


def _translate(exc: BaseException) -> AzureError:
    """Map an upstream Azure SDK exception to :class:`AzureError`."""
    if isinstance(exc, ClientAuthenticationError):
        return AzureError(
            f"Azure authentication failed: {exc}",
            code="AUTH_ERROR",
            status_code=401,
        )
    if isinstance(exc, ResourceNotFoundError):
        return AzureError(
            str(exc) or "Azure resource not found",
            code="NOT_FOUND",
            status_code=getattr(exc, "status_code", 404) or 404,
        )
    if isinstance(exc, HttpResponseError):
        sc = int(getattr(exc, "status_code", 0) or 0)
        code = _HTTP_CODE_MAP.get(sc, "UPSTREAM_ERROR")
        msg = getattr(exc, "message", None) or str(exc) or "Azure HTTP error"
        # Keep messages reasonably short
        if len(msg) > 500:
            msg = msg[:500] + "…"
        return AzureError(f"{msg} (HTTP {sc})", code=code, status_code=sc)
    if isinstance(exc, ServiceRequestTimeoutError):
        return AzureError(
            f"Azure request timed out: {exc}", code="TIMEOUT"
        )
    if isinstance(exc, ServiceRequestError):
        return AzureError(
            f"Azure transport error: {exc}", code="CONNECTION_ERROR"
        )
    if isinstance(exc, _SDKAzureError):
        return AzureError(
            f"Azure SDK error: {exc}", code="AZURE_ERROR"
        )
    return AzureError(f"Unexpected Azure error: {exc}", code="AZURE_ERROR")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def build_azure_client(config: dict) -> "AzureClient":
    """Build a :class:`AzureClient` from a tool config dict.

    Use as an async context manager so credentials and SDK clients are
    closed cleanly::

        async with build_azure_client(config) as client:
            sub_id = client.resolve_subscription(params)
            ...
    """
    return AzureClient(config)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AzureClient:
    """Async wrapper holding the credential and lazy SDK client cache.

    Sub-clients that require a subscription ID are keyed by ``sub_id``
    (per call) — the caller passes ``client.compute(sub_id)`` etc.
    Subscription-less clients (Cost Management, Subscription,
    ResourceSubscription) are cached singletons.
    """

    def __init__(self, config: dict) -> None:
        self._cfg = dict(config or {})
        self._timeout: float = float(self._cfg.get("timeout") or 60)
        env_name = self._cfg.get("cloud_environment") or "AzurePublicCloud"
        env = _CLOUD_ENVIRONMENTS.get(env_name)
        if env is None:
            raise AzureError(
                f"Unknown cloud_environment {env_name!r}.",
                code="CONFIG_MISSING",
            )
        self._env = env
        self._credential: Optional[ClientSecretCredential] = None
        # Caches keyed by subscription_id
        self._compute: dict[str, ComputeManagementClient] = {}
        self._network: dict[str, NetworkManagementClient] = {}
        self._monitor: dict[str, MonitorManagementClient] = {}
        self._resource: dict[str, ResourceManagementClient] = {}
        self._sql: dict[str, SqlManagementClient] = {}
        self._web: dict[str, WebSiteManagementClient] = {}
        self._aks: dict[str, ContainerServiceClient] = {}
        self._storage: dict[str, StorageManagementClient] = {}
        self._dtl: dict[str, DevTestLabsClient] = {}
        # Subscription-less clients
        self._subscription: Optional[SubscriptionClient] = None
        self._res_sub: Optional[ResourceSubscriptionClient] = None
        self._cost: Optional[CostManagementClient] = None
        self._advisor: Optional[AdvisorManagementClient] = None
        self._closed = False

    # ── Context manager ─────────────────────────────────────────────────────

    async def __aenter__(self) -> "AzureClient":
        tenant = (self._cfg.get("tenant_id") or "").strip()
        client_id = (self._cfg.get("client_id") or "").strip()
        secret = self._cfg.get("client_secret") or ""
        if not (tenant and client_id and secret):
            raise AzureError(
                "Azure config missing required fields (tenant_id, "
                "client_id, client_secret).",
                code="CONFIG_MISSING",
            )
        try:
            self._credential = ClientSecretCredential(
                tenant_id=tenant,
                client_id=client_id,
                client_secret=secret,
                authority=self._env["active_directory"],
            )
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
        # Close every cached client, then the credential.
        clients: list[Any] = []
        for cache in (
            self._compute, self._network, self._monitor, self._resource,
            self._sql, self._web, self._aks, self._storage, self._dtl,
        ):
            clients.extend(cache.values())
            cache.clear()
        for solo in (
            self._subscription, self._res_sub, self._cost, self._advisor,
        ):
            if solo is not None:
                clients.append(solo)
        self._subscription = None
        self._res_sub = None
        self._cost = None
        self._advisor = None
        for c in clients:
            try:
                await c.close()
            except Exception:
                log.debug("azure: error closing SDK client", exc_info=True)
        if self._credential is not None:
            try:
                await self._credential.close()
            except Exception:
                log.debug("azure: error closing credential", exc_info=True)
            self._credential = None

    # ── Internal helpers ────────────────────────────────────────────────────

    def _cred(self) -> ClientSecretCredential:
        if self._credential is None:
            raise AzureError(
                "AzureClient must be used as an async context manager.",
                code="AZURE_ERROR",
            )
        return self._credential

    @property
    def base_url(self) -> str:
        return self._env["resource_manager"]

    @property
    def credential_scopes(self) -> tuple[str, ...]:
        return self._env["credential_scopes"]

    @property
    def default_resource_group(self) -> str:
        return str(self._cfg.get("default_resource_group") or "")

    @property
    def default_subscription_id(self) -> str:
        return str(self._cfg.get("default_subscription_id") or "")

    def resolve_subscription(self, params: Optional[dict]) -> str:
        """Resolve the effective subscription_id for a call."""
        sub = ""
        if params:
            sub = (params.get("subscription_id") or "").strip()
        if not sub:
            sub = self.default_subscription_id
        if not sub:
            raise AzureError(
                "subscription_id is required (set "
                "'default_subscription_id' in the profile or pass "
                "'subscription_id' in params).",
                code="INVALID_PARAMS",
            )
        return sub

    # ── Sub-client accessors (cached per sub_id) ────────────────────────────

    def compute(self, sub_id: str) -> ComputeManagementClient:
        c = self._compute.get(sub_id)
        if c is None:
            c = ComputeManagementClient(
                self._cred(), sub_id, base_url=self.base_url
            )
            self._compute[sub_id] = c
        return c

    def network(self, sub_id: str) -> NetworkManagementClient:
        c = self._network.get(sub_id)
        if c is None:
            c = NetworkManagementClient(
                self._cred(), sub_id, base_url=self.base_url
            )
            self._network[sub_id] = c
        return c

    def monitor(self, sub_id: str) -> MonitorManagementClient:
        c = self._monitor.get(sub_id)
        if c is None:
            c = MonitorManagementClient(
                self._cred(), sub_id, base_url=self.base_url
            )
            self._monitor[sub_id] = c
        return c

    def resource(self, sub_id: str) -> ResourceManagementClient:
        c = self._resource.get(sub_id)
        if c is None:
            c = ResourceManagementClient(
                self._cred(), sub_id, base_url=self.base_url
            )
            self._resource[sub_id] = c
        return c

    def sql(self, sub_id: str) -> SqlManagementClient:
        c = self._sql.get(sub_id)
        if c is None:
            c = SqlManagementClient(
                self._cred(), sub_id, base_url=self.base_url
            )
            self._sql[sub_id] = c
        return c

    def web(self, sub_id: str) -> WebSiteManagementClient:
        c = self._web.get(sub_id)
        if c is None:
            c = WebSiteManagementClient(
                self._cred(), sub_id, base_url=self.base_url
            )
            self._web[sub_id] = c
        return c

    def aks(self, sub_id: str) -> ContainerServiceClient:
        c = self._aks.get(sub_id)
        if c is None:
            c = ContainerServiceClient(
                self._cred(), sub_id, base_url=self.base_url
            )
            self._aks[sub_id] = c
        return c

    def storage(self, sub_id: str) -> StorageManagementClient:
        c = self._storage.get(sub_id)
        if c is None:
            c = StorageManagementClient(
                self._cred(), sub_id, base_url=self.base_url
            )
            self._storage[sub_id] = c
        return c

    def devtestlabs(self, sub_id: str) -> DevTestLabsClient:
        c = self._dtl.get(sub_id)
        if c is None:
            c = DevTestLabsClient(
                self._cred(), sub_id, base_url=self.base_url
            )
            self._dtl[sub_id] = c
        return c

    def subscriptions(self) -> SubscriptionClient:
        if self._subscription is None:
            self._subscription = SubscriptionClient(
                self._cred(), base_url=self.base_url
            )
        return self._subscription

    def resource_subscriptions(self) -> ResourceSubscriptionClient:
        if self._res_sub is None:
            self._res_sub = ResourceSubscriptionClient(
                self._cred(), base_url=self.base_url
            )
        return self._res_sub

    def cost(self) -> CostManagementClient:
        if self._cost is None:
            self._cost = CostManagementClient(
                self._cred(), base_url=self.base_url
            )
        return self._cost

    def advisor(self, sub_id: str) -> AdvisorManagementClient:
        c = self._advisor
        if c is None or getattr(c, "_subscription_id", None) != sub_id:
            # Advisor is per-subscription
            c = AdvisorManagementClient(
                self._cred(), sub_id, base_url=self.base_url
            )
            self._advisor = c
        return c

    # ── Generic call wrapper ────────────────────────────────────────────────

    @staticmethod
    async def collect(
        async_iter: AsyncIterable[Any], *, limit: Optional[int] = None
    ) -> list[Any]:
        """Drain an Azure SDK ``AsyncItemPaged`` into a list, cap at ``limit``."""
        out: list[Any] = []
        try:
            async for item in async_iter:
                out.append(item)
                if limit is not None and len(out) >= limit:
                    break
        except Exception as exc:
            raise _translate(exc) from exc
        return out

    @staticmethod
    async def call(coro_or_value: Any) -> Any:
        """Await ``coro_or_value`` translating any Azure exception."""
        try:
            return await coro_or_value
        except Exception as exc:
            raise _translate(exc) from exc

    # ── Serialisation ───────────────────────────────────────────────────────

    @staticmethod
    def serialize(obj: Any) -> Any:
        """Convert an SDK model into a plain JSON-able dict."""
        if obj is None:
            return None
        as_dict = getattr(obj, "as_dict", None)
        if callable(as_dict):
            try:
                return as_dict()
            except Exception:
                pass
        if isinstance(obj, dict):
            return obj
        return obj


# Re-export for callers
__all__ = [
    "AZURE_CONFIG_DEFAULTS",
    "AZURE_CONFIG_SCHEMA",
    "AzureClient",
    "AzureError",
    "build_azure_client",
]
