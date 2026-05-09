"""gSage AI — Kubernetes write operations (HITL approval required).

Exposes a small, explicit set of recovery actions Tier 1/2 analysts may
need to take during a triage:

- ``restart_deployment``: triggers a rolling restart by patching the pod
  template annotation ``kubectl.kubernetes.io/restartedAt`` (the same
  mechanism ``kubectl rollout restart`` uses).
- ``scale_deployment``: changes ``spec.replicas`` (0–50) via the
  ``/scale`` subresource.
- ``delete_pod``: deletes a single pod, optionally with a custom
  ``grace_period_seconds``. The pod's controller (Deployment/StatefulSet)
  is responsible for recreating it.

Design constraints:

- All actions require both ``namespace`` and ``name``. Wildcards and
  label-selector-based batch operations are intentionally **not**
  supported — destructive scope must be explicit.
- ``requires_approval=True`` so the agent layer issues a HITL prompt
  before invocation.
- Audit captures ``namespace``, ``name`` and ``action``.

Permission: ``k8s:write``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.devops.k8s._client import (
    K8S_CONFIG_DEFAULTS,
    K8S_CONFIG_SCHEMA,
    K8sClient,
    K8sError,
    build_k8s_client,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = frozenset({
    "restart_deployment",
    "scale_deployment",
    "delete_pod",
})

_MAX_REPLICAS = 50
_DEFAULT_GRACE_PERIOD = 30


class _ParamError(Exception):
    pass


class K8sManageTool(BaseTool):
    """Mutating Kubernetes actions with human-in-the-loop approval.

    All actions are scoped to a single ``namespace + name`` target.
    No batch / label-selector mode is supported.

    Permission: ``k8s:write``.
    """

    name: ClassVar[str] = "k8s_manage"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Kubernetes write actions: restart_deployment, scale_deployment, "
        "delete_pod. Requires human approval. No batch operations."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "kubernetes"
    permissions: ClassVar[list[str]] = ["k8s:write"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "target_entities": "name",
        "target_resource": "namespace",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action", "namespace", "name"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which mutating operation to perform.",
            },
            "profile": {
                "type": "string",
                "description": (
                    "GSageToolConfig profile (cluster) to use. Omit for the "
                    "'default' profile."
                ),
            },
            "namespace": {
                "type": "string",
                "description": "Target namespace (required).",
            },
            "name": {
                "type": "string",
                "description": (
                    "[restart_deployment, scale_deployment] Deployment name. "
                    "[delete_pod] Pod name."
                ),
            },
            "replicas": {
                "type": "integer",
                "minimum": 0,
                "maximum": _MAX_REPLICAS,
                "description": (
                    f"[scale_deployment] Desired replica count (0–{_MAX_REPLICAS})."
                ),
            },
            "grace_period_seconds": {
                "type": "integer",
                "minimum": 0,
                "maximum": 600,
                "description": (
                    f"[delete_pod] Termination grace period in seconds "
                    f"(default {_DEFAULT_GRACE_PERIOD}). Use 0 for force-delete."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Free-text justification for the change. Recorded in the "
                    "audit log to support post-incident review."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = K8S_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = K8S_CONFIG_DEFAULTS
    state_schema: ClassVar[Optional[dict]] = None
    state_defaults: ClassVar[dict] = {}
    reset_policy: ClassVar[str] = "never"

    # ── Execute ─────────────────────────────────────────────────────────────

    async def execute(
        self,
        agent_context: AgentContext,
        params: dict,
        config: dict,
        state: dict,
    ) -> ToolResult:
        t0 = time.monotonic()
        action = (params.get("action") or "").strip()
        if action not in _ACTIONS:
            return self._failure(
                "INVALID_PARAMS",
                f"action must be one of {sorted(_ACTIONS)}; got {action!r}.",
            )

        try:
            namespace = _require(params, "namespace")
            name = _require(params, "name")
            async with build_k8s_client(config) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(client, namespace, name, params)
        except _ParamError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INVALID_PARAMS", str(exc), execution_time_ms=elapsed
            )
        except K8sError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code, str(exc), execution_time_ms=elapsed
            )
        except Exception as exc:
            log.exception("k8s_manage(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={
                "action": action,
                "namespace": namespace,
                "name": name,
                "reason": params.get("reason"),
                **data,
            },
            execution_time_ms=elapsed,
        )

    # ── Action handlers ─────────────────────────────────────────────────────

    async def _do_restart_deployment(
        self,
        client: K8sClient,
        namespace: str,
        name: str,
        params: dict,
    ) -> dict:
        """Trigger a rolling restart by bumping the restartedAt annotation."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now
                        }
                    }
                }
            }
        }
        result = await client.patch_deployment(namespace, name, patch)
        spec = (result or {}).get("spec") or {}
        return {
            "result": "patched",
            "restarted_at": now,
            "replicas_desired": spec.get("replicas"),
        }

    async def _do_scale_deployment(
        self,
        client: K8sClient,
        namespace: str,
        name: str,
        params: dict,
    ) -> dict:
        replicas = params.get("replicas")
        if replicas is None:
            raise _ParamError(
                "'replicas' is required for action=scale_deployment."
            )
        replicas = int(replicas)
        if replicas < 0 or replicas > _MAX_REPLICAS:
            raise _ParamError(
                f"'replicas' must be between 0 and {_MAX_REPLICAS}."
            )

        # Capture previous replica count for the audit/result payload
        previous: Optional[int] = None
        try:
            cur = await client.read_deployment(namespace, name)
            previous = ((cur or {}).get("spec") or {}).get("replicas")
        except K8sError as exc:
            log.debug("scale_deployment: read previous failed: %s", exc)

        await client.patch_deployment_scale(namespace, name, replicas)
        return {
            "result": "scaled",
            "replicas_previous": previous,
            "replicas_desired": replicas,
        }

    async def _do_delete_pod(
        self,
        client: K8sClient,
        namespace: str,
        name: str,
        params: dict,
    ) -> dict:
        grace = params.get("grace_period_seconds")
        grace_int = (
            int(grace) if grace is not None else _DEFAULT_GRACE_PERIOD
        )
        await client.delete_pod(namespace, name, grace_period_seconds=grace_int)
        return {
            "result": "deleted",
            "grace_period_seconds": grace_int,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require(params: dict, field: str) -> str:
    val = params.get(field)
    if isinstance(val, str):
        val = val.strip()
    if not val:
        raise _ParamError(f"'{field}' is required.")
    return str(val)


_ = Any
