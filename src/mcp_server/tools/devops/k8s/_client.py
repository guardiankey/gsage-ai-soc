"""gSage AI — Kubernetes async client wrapper.

The official ``kubernetes`` Python library is synchronous; we wrap each
call in :func:`asyncio.to_thread` so individual MCP tool requests don't
block the event loop. This is an acceptable trade-off for Tier 1/2
investigative usage (low concurrency, interactive calls).

Configuration: discrete fields are preferred over a full kubeconfig YAML
to keep the admin-console UX simple and to play well with the encrypted
``GSageToolConfig`` JSONB storage. Fields:

- ``api_server``: cluster API URL, e.g. ``https://k8s.example.com:6443``.
- ``token``: service-account bearer token (sensitive).
- ``ca_cert``: PEM-encoded CA certificate of the API server.
- ``verify_tls``: whether to verify the API-server TLS certificate.
- ``default_namespace``: namespace assumed when the caller omits one.
- ``timeout``: HTTP request timeout in seconds.
- ``in_cluster``: when ``true``, ignore the other fields and use the
  pod's mounted ServiceAccount (``/var/run/secrets/...``).

Usage::

    async with build_k8s_client(config) as client:
        pods = await client.list_pods("default")

All upstream errors are translated to :class:`K8sError` with a stable
``code`` so tool callers can pattern-match without catching the upstream
``ApiException`` type.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from types import TracebackType
from typing import Any, Optional

from kubernetes import client as kclient  # type: ignore[import-untyped]
from kubernetes import config as kconfig  # type: ignore[import-untyped]
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration schema / defaults (shared by k8s_observe, k8s_manage,
# k8s_dashboard).
# ---------------------------------------------------------------------------

K8S_CONFIG_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "api_server": {
            "type": "string",
            "description": (
                "Cluster API server URL, e.g. 'https://k8s.example.com:6443'. "
                "Required unless 'in_cluster' is true."
            ),
        },
        "token": {
            "type": "string",
            "description": (
                "Service account bearer token (sensitive). Required unless "
                "'in_cluster' is true."
            ),
        },
        "ca_cert": {
            "type": "string",
            "description": (
                "PEM-encoded CA certificate of the API server. Optional but "
                "recommended for self-signed clusters; ignored when "
                "'verify_tls' is false."
            ),
        },
        "verify_tls": {
            "type": "boolean",
            "description": (
                "Verify the API-server TLS certificate (default: true). "
                "Disable only for local/test clusters."
            ),
        },
        "default_namespace": {
            "type": "string",
            "description": (
                "Namespace assumed when the caller omits one (default: "
                "'default')."
            ),
        },
        "timeout": {
            "type": "integer",
            "minimum": 5,
            "maximum": 300,
            "description": "HTTP request timeout in seconds (default: 30).",
        },
        "in_cluster": {
            "type": "boolean",
            "description": (
                "When true, use the pod's mounted ServiceAccount credentials "
                "(ignores api_server / token / ca_cert). Default: false."
            ),
        },
    },
    "additionalProperties": False,
}

K8S_CONFIG_DEFAULTS: dict = {
    "api_server": "",
    "token": "",
    "ca_cert": "",
    "verify_tls": True,
    "default_namespace": "default",
    "timeout": 30,
    "in_cluster": False,
}


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class K8sError(Exception):
    """Raised for Kubernetes API or transport errors.

    Attributes
    ----------
    code:
        Stable agent-friendly error code:
        ``AUTH_ERROR`` | ``FORBIDDEN`` | ``NOT_FOUND`` | ``CONFLICT`` |
        ``INVALID_PARAMS`` | ``RATE_LIMITED`` | ``UPSTREAM_ERROR`` |
        ``CONNECTION_ERROR`` | ``CONFIG_MISSING`` | ``TIMEOUT`` |
        ``K8S_ERROR``.
    status_code:
        HTTP status code; 0 for transport / config errors.
    """

    def __init__(
        self,
        message: str,
        code: str = "K8S_ERROR",
        status_code: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


_HTTP_CODE_MAP = {
    401: "AUTH_ERROR",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "INVALID_PARAMS",
    429: "RATE_LIMITED",
}


def _translate_api_exception(exc: ApiException) -> K8sError:
    """Map a ``kubernetes.client.ApiException`` to :class:`K8sError`."""
    sc = int(exc.status or 0)
    code = _HTTP_CODE_MAP.get(sc, "UPSTREAM_ERROR")
    # ApiException.body is usually a JSON string with {"message": ...}
    msg = exc.reason or "Kubernetes API error"
    body = getattr(exc, "body", None)
    if body:
        try:
            import json as _json

            data = _json.loads(body) if isinstance(body, str) else body
            if isinstance(data, dict):
                msg = data.get("message") or msg
        except Exception:
            pass
    return K8sError(f"{msg} (HTTP {sc})", code=code, status_code=sc)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def build_k8s_client(config: dict) -> "K8sClient":
    """Build a :class:`K8sClient` from a tool config dict.

    Use as an async context manager so the temp CA file (if any) is cleaned
    up::

        async with build_k8s_client(config) as client:
            ...
    """
    return K8sClient(config)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class K8sClient:
    """Async wrapper around the synchronous ``kubernetes`` client.

    All methods schedule the underlying call in a worker thread via
    :func:`asyncio.to_thread`, returning the parsed Python object (already
    converted by the upstream SDK).
    """

    def __init__(self, config: dict) -> None:
        self._cfg = dict(config or {})
        self._api_client: Optional[kclient.ApiClient] = None
        self._ca_file: Optional[str] = None
        self._timeout: float = float(self._cfg.get("timeout") or 30)

    # ── Context manager ─────────────────────────────────────────────────────

    async def __aenter__(self) -> "K8sClient":
        await asyncio.to_thread(self._build_sync)
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        if self._api_client is not None:
            try:
                self._api_client.close()
            except Exception:
                pass
            self._api_client = None
        if self._ca_file:
            try:
                os.unlink(self._ca_file)
            except OSError:
                pass
            self._ca_file = None

    # ── Build ───────────────────────────────────────────────────────────────

    def _build_sync(self) -> None:
        cfg = self._cfg
        if cfg.get("in_cluster"):
            try:
                kconfig.load_incluster_config()
            except Exception as exc:
                raise K8sError(
                    f"Could not load in-cluster config: {exc}",
                    code="CONFIG_MISSING",
                ) from exc
            # kubernetes.config sets a global default; clone it for isolation
            cfg_obj = kclient.Configuration.get_default_copy()
        else:
            api_server = (cfg.get("api_server") or "").rstrip("/")
            token = cfg.get("token") or ""
            if not api_server:
                raise K8sError(
                    "Kubernetes 'api_server' is not configured.",
                    code="CONFIG_MISSING",
                )
            if not token:
                raise K8sError(
                    "Kubernetes 'token' is not configured.",
                    code="CONFIG_MISSING",
                )
            cfg_obj = kclient.Configuration()
            cfg_obj.host = api_server
            cfg_obj.api_key = {"authorization": f"Bearer {token}"}
            verify = bool(cfg.get("verify_tls", True))
            cfg_obj.verify_ssl = verify
            ca_pem = cfg.get("ca_cert") or ""
            if verify and ca_pem:
                fh = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".pem", delete=False
                )
                try:
                    fh.write(ca_pem)
                    fh.flush()
                    self._ca_file = fh.name
                finally:
                    fh.close()
                cfg_obj.ssl_ca_cert = self._ca_file  # type: ignore[assignment]
            elif not verify:
                cfg_obj.verify_ssl = False
                # Suppress urllib3 InsecureRequestWarning for unverified TLS
                import urllib3  # noqa: PLC0415

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._api_client = kclient.ApiClient(configuration=cfg_obj)

    # ── Internal accessors ──────────────────────────────────────────────────

    def _api(self) -> kclient.ApiClient:
        if self._api_client is None:
            raise K8sError(
                "K8sClient must be used as an async context manager.",
                code="K8S_ERROR",
            )
        return self._api_client

    @property
    def core_v1(self) -> kclient.CoreV1Api:
        return kclient.CoreV1Api(self._api())

    @property
    def apps_v1(self) -> kclient.AppsV1Api:
        return kclient.AppsV1Api(self._api())

    @property
    def custom(self) -> kclient.CustomObjectsApi:
        return kclient.CustomObjectsApi(self._api())

    @property
    def default_namespace(self) -> str:
        return str(self._cfg.get("default_namespace") or "default")

    # ── Generic call helper ─────────────────────────────────────────────────

    async def _call(self, fn, *args, **kwargs) -> Any:
        """Run a synchronous SDK call in a worker thread, mapping errors."""
        kwargs.setdefault("_request_timeout", self._timeout)
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ApiException as exc:
            raise _translate_api_exception(exc) from exc
        except K8sError:
            raise
        except Exception as exc:
            raise K8sError(
                f"Kubernetes call failed: {exc}", code="CONNECTION_ERROR"
            ) from exc

    # ── Sanitised serializer ────────────────────────────────────────────────

    def serialize(self, obj: Any) -> Any:
        """Convert an SDK model to a plain dict using the API client.

        Falls back to ``obj.to_dict()`` for SDK models or returns ``obj``
        unchanged otherwise.
        """
        if obj is None:
            return None
        api = self._api()
        sanitize = getattr(api, "sanitize_for_serialization", None)
        if sanitize is not None:
            try:
                return sanitize(obj)
            except Exception:
                pass
        if hasattr(obj, "to_dict"):
            try:
                return obj.to_dict()
            except Exception:
                pass
        return obj

    # ── Namespaces / nodes ──────────────────────────────────────────────────

    async def list_namespaces(self) -> list[dict]:
        res = await self._call(self.core_v1.list_namespace)
        return [self.serialize(i) for i in (res.items or [])]

    async def list_nodes(self) -> list[dict]:
        res = await self._call(self.core_v1.list_node)
        return [self.serialize(i) for i in (res.items or [])]

    # ── Pods ────────────────────────────────────────────────────────────────

    async def list_pods(
        self,
        namespace: Optional[str] = None,
        label_selector: Optional[str] = None,
        field_selector: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        kwargs: dict = {}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if field_selector:
            kwargs["field_selector"] = field_selector
        if limit:
            kwargs["limit"] = int(limit)
        if namespace:
            res = await self._call(
                self.core_v1.list_namespaced_pod, namespace, **kwargs
            )
        else:
            res = await self._call(
                self.core_v1.list_pod_for_all_namespaces, **kwargs
            )
        return [self.serialize(i) for i in (res.items or [])]

    async def read_pod(self, namespace: str, name: str) -> dict:
        res = await self._call(
            self.core_v1.read_namespaced_pod, name, namespace
        )
        return self.serialize(res)

    async def read_pod_log(
        self,
        namespace: str,
        name: str,
        *,
        container: Optional[str] = None,
        tail_lines: Optional[int] = None,
        since_seconds: Optional[int] = None,
        previous: bool = False,
        timestamps: bool = False,
        limit_bytes: Optional[int] = None,
    ) -> str:
        kwargs: dict = {}
        if container:
            kwargs["container"] = container
        if tail_lines is not None:
            kwargs["tail_lines"] = int(tail_lines)
        if since_seconds is not None:
            kwargs["since_seconds"] = int(since_seconds)
        if previous:
            kwargs["previous"] = True
        if timestamps:
            kwargs["timestamps"] = True
        if limit_bytes is not None:
            kwargs["limit_bytes"] = int(limit_bytes)
        return await self._call(
            self.core_v1.read_namespaced_pod_log, name, namespace, **kwargs
        )

    async def delete_pod(
        self,
        namespace: str,
        name: str,
        grace_period_seconds: Optional[int] = None,
    ) -> dict:
        body = kclient.V1DeleteOptions(
            grace_period_seconds=grace_period_seconds
        )
        res = await self._call(
            self.core_v1.delete_namespaced_pod,
            name,
            namespace,
            body=body,
        )
        return self.serialize(res)

    # ── Events ──────────────────────────────────────────────────────────────

    async def list_events(
        self,
        namespace: Optional[str] = None,
        field_selector: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        kwargs: dict = {}
        if field_selector:
            kwargs["field_selector"] = field_selector
        if limit:
            kwargs["limit"] = int(limit)
        if namespace:
            res = await self._call(
                self.core_v1.list_namespaced_event, namespace, **kwargs
            )
        else:
            res = await self._call(
                self.core_v1.list_event_for_all_namespaces, **kwargs
            )
        return [self.serialize(i) for i in (res.items or [])]

    # ── Deployments ─────────────────────────────────────────────────────────

    async def list_deployments(
        self,
        namespace: Optional[str] = None,
        label_selector: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        kwargs: dict = {}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if limit:
            kwargs["limit"] = int(limit)
        if namespace:
            res = await self._call(
                self.apps_v1.list_namespaced_deployment, namespace, **kwargs
            )
        else:
            res = await self._call(
                self.apps_v1.list_deployment_for_all_namespaces, **kwargs
            )
        return [self.serialize(i) for i in (res.items or [])]

    async def read_deployment(self, namespace: str, name: str) -> dict:
        res = await self._call(
            self.apps_v1.read_namespaced_deployment, name, namespace
        )
        return self.serialize(res)

    async def patch_deployment(
        self, namespace: str, name: str, patch: dict
    ) -> dict:
        res = await self._call(
            self.apps_v1.patch_namespaced_deployment,
            name,
            namespace,
            patch,
        )
        return self.serialize(res)

    async def patch_deployment_scale(
        self, namespace: str, name: str, replicas: int
    ) -> dict:
        body = {"spec": {"replicas": int(replicas)}}
        res = await self._call(
            self.apps_v1.patch_namespaced_deployment_scale,
            name,
            namespace,
            body,
        )
        return self.serialize(res)

    # ── Services ────────────────────────────────────────────────────────────

    async def list_services(
        self,
        namespace: Optional[str] = None,
        label_selector: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        kwargs: dict = {}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if limit:
            kwargs["limit"] = int(limit)
        if namespace:
            res = await self._call(
                self.core_v1.list_namespaced_service, namespace, **kwargs
            )
        else:
            res = await self._call(
                self.core_v1.list_service_for_all_namespaces, **kwargs
            )
        return [self.serialize(i) for i in (res.items or [])]

    # ── Metrics (metrics.k8s.io custom resource) ────────────────────────────

    async def top_pods(self, namespace: Optional[str] = None) -> list[dict]:
        """List pod metrics from ``metrics.k8s.io``.

        Raises :class:`K8sError` with code ``UPSTREAM_ERROR`` and status
        404 when the metrics-server is not installed.
        """
        try:
            if namespace:
                res = await self._call(
                    self.custom.list_namespaced_custom_object,
                    "metrics.k8s.io",
                    "v1beta1",
                    namespace,
                    "pods",
                )
            else:
                res = await self._call(
                    self.custom.list_cluster_custom_object,
                    "metrics.k8s.io",
                    "v1beta1",
                    "pods",
                )
        except K8sError:
            raise
        return list((res or {}).get("items") or [])

    async def top_nodes(self) -> list[dict]:
        """List node metrics from ``metrics.k8s.io``."""
        res = await self._call(
            self.custom.list_cluster_custom_object,
            "metrics.k8s.io",
            "v1beta1",
            "nodes",
        )
        return list((res or {}).get("items") or [])
