"""gSage AI — Kubernetes read-only inspection tool.

Exposes an ``action`` enum dispatcher covering the most common
investigative operations Tier 1 / Tier 2 analysts need when triaging an
incident on a workload running in Kubernetes:

- ``list_namespaces``, ``list_nodes``, ``list_services``
- ``list_deployments``, ``list_pods``
- ``describe_pod``         — status, conditions, containers, restarts, probes
- ``get_logs``             — last N lines, optional container, since, previous
- ``get_events``           — by namespace and/or involved object
- ``get_rollout_status``   — Deployment progress vs ``status.conditions``
- ``top_pods`` / ``top_nodes`` — metrics.k8s.io (gracefully fails if absent)

Tabular results (``list_*``, ``get_events``) are run through the shared
``result_export`` pipeline: when the result exceeds 100 rows, a CSV
artifact is generated automatically and only the first 100 rows are
inlined for the agent. The same flag ``export_csv=true`` lets the caller
force CSV generation for any size.

Permission: ``k8s:read``. Multi-cluster via ``params.profile``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.devops.k8s._client import (
    K8S_CONFIG_DEFAULTS,
    K8S_CONFIG_SCHEMA,
    K8sClient,
    K8sError,
    build_k8s_client,
)
from src.mcp_server.tools.result_export import (
    AGENT_PREVIEW_ROWS,
    build_agent_payload,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = frozenset({
    "list_namespaces",
    "list_nodes",
    "list_services",
    "list_deployments",
    "list_pods",
    "describe_pod",
    "get_logs",
    "get_events",
    "get_rollout_status",
    "top_pods",
    "top_nodes",
})

_DEFAULT_RESULTS = 100
_MAX_RESULTS = 500
_LOG_TAIL_DEFAULT = 200
_LOG_TAIL_MAX = 2000
_LOG_BYTES_LIMIT = 1_000_000  # 1 MB safeguard


# ---------------------------------------------------------------------------
# Slim views
# ---------------------------------------------------------------------------


def _meta(obj: dict) -> dict:
    return obj.get("metadata") or {}


def _slim_namespace(ns: dict) -> dict:
    m = _meta(ns)
    return {
        "name": m.get("name"),
        "phase": (ns.get("status") or {}).get("phase"),
        "labels": m.get("labels"),
        "created_at": m.get("creationTimestamp") or m.get("creation_timestamp"),
    }


def _slim_node(node: dict) -> dict:
    m = _meta(node)
    status = node.get("status") or {}
    conditions = status.get("conditions") or []
    ready = next(
        (c for c in conditions if c.get("type") == "Ready"),
        None,
    )
    capacity = status.get("capacity") or {}
    allocatable = status.get("allocatable") or {}
    addresses = status.get("addresses") or []
    info = status.get("nodeInfo") or status.get("node_info") or {}
    return {
        "name": m.get("name"),
        "ready": (ready or {}).get("status") == "True" if ready else None,
        "ready_reason": (ready or {}).get("reason"),
        "kubelet_version": info.get("kubeletVersion") or info.get("kubelet_version"),
        "os_image": info.get("osImage") or info.get("os_image"),
        "container_runtime": info.get("containerRuntimeVersion")
        or info.get("container_runtime_version"),
        "internal_ip": next(
            (a.get("address") for a in addresses if a.get("type") == "InternalIP"),
            None,
        ),
        "capacity": {
            "cpu": capacity.get("cpu"),
            "memory": capacity.get("memory"),
            "pods": capacity.get("pods"),
        },
        "allocatable": {
            "cpu": allocatable.get("cpu"),
            "memory": allocatable.get("memory"),
        },
        "taints": [
            {"key": t.get("key"), "effect": t.get("effect"), "value": t.get("value")}
            for t in (node.get("spec") or {}).get("taints") or []
        ],
        "labels": m.get("labels"),
        "created_at": m.get("creationTimestamp") or m.get("creation_timestamp"),
    }


def _slim_deployment(dep: dict) -> dict:
    m = _meta(dep)
    spec = dep.get("spec") or {}
    status = dep.get("status") or {}
    return {
        "namespace": m.get("namespace"),
        "name": m.get("name"),
        "replicas_desired": spec.get("replicas"),
        "replicas_ready": status.get("readyReplicas") or status.get("ready_replicas"),
        "replicas_updated": status.get("updatedReplicas")
        or status.get("updated_replicas"),
        "replicas_available": status.get("availableReplicas")
        or status.get("available_replicas"),
        "replicas_unavailable": status.get("unavailableReplicas")
        or status.get("unavailable_replicas"),
        "strategy": (spec.get("strategy") or {}).get("type"),
        "image": _first_container_image(spec),
        "labels": m.get("labels"),
        "created_at": m.get("creationTimestamp") or m.get("creation_timestamp"),
    }


def _first_container_image(spec: dict) -> Optional[str]:
    template = (spec.get("template") or {}).get("spec") or {}
    containers = template.get("containers") or []
    if containers:
        return containers[0].get("image")
    return None


def _container_status_summary(cs: dict) -> dict:
    """Summarise a V1ContainerStatus dict to the most useful fields."""
    state = cs.get("state") or {}
    last_state = cs.get("lastState") or cs.get("last_state") or {}
    waiting = state.get("waiting") or {}
    terminated = state.get("terminated") or {}
    last_term = last_state.get("terminated") or {}
    if waiting:
        cur = {"phase": "waiting", "reason": waiting.get("reason"),
               "message": (waiting.get("message") or "")[:200] or None}
    elif terminated:
        cur = {"phase": "terminated", "reason": terminated.get("reason"),
               "exit_code": terminated.get("exitCode")
               or terminated.get("exit_code"),
               "finished_at": terminated.get("finishedAt")
               or terminated.get("finished_at")}
    elif state.get("running"):
        cur = {"phase": "running",
               "started_at": (state.get("running") or {}).get("startedAt")
               or (state.get("running") or {}).get("started_at")}
    else:
        cur = {"phase": "unknown"}
    return {
        "name": cs.get("name"),
        "image": cs.get("image"),
        "ready": cs.get("ready"),
        "restart_count": cs.get("restartCount") or cs.get("restart_count") or 0,
        "started": cs.get("started"),
        "state": cur,
        "last_terminated_reason": last_term.get("reason") if last_term else None,
        "last_terminated_exit_code": (
            last_term.get("exitCode") or last_term.get("exit_code")
            if last_term else None
        ),
    }


def _slim_pod(pod: dict) -> dict:
    m = _meta(pod)
    spec = pod.get("spec") or {}
    status = pod.get("status") or {}
    container_statuses = (
        status.get("containerStatuses") or status.get("container_statuses") or []
    )
    total_restarts = sum(
        int(cs.get("restartCount") or cs.get("restart_count") or 0)
        for cs in container_statuses
    )
    return {
        "namespace": m.get("namespace"),
        "name": m.get("name"),
        "phase": status.get("phase"),
        "node": spec.get("nodeName") or spec.get("node_name"),
        "pod_ip": status.get("podIP") or status.get("pod_ip"),
        "host_ip": status.get("hostIP") or status.get("host_ip"),
        "ready_containers": sum(1 for cs in container_statuses if cs.get("ready")),
        "total_containers": len(container_statuses),
        "restart_count": total_restarts,
        "qos_class": status.get("qosClass") or status.get("qos_class"),
        "start_time": status.get("startTime") or status.get("start_time"),
        "created_at": m.get("creationTimestamp") or m.get("creation_timestamp"),
        "labels": m.get("labels"),
        "owner": _pod_owner(m),
    }


def _pod_owner(metadata: dict) -> Optional[str]:
    refs = metadata.get("ownerReferences") or metadata.get("owner_references") or []
    if not refs:
        return None
    r = refs[0]
    return f"{r.get('kind')}/{r.get('name')}"


def _slim_service(svc: dict) -> dict:
    m = _meta(svc)
    spec = svc.get("spec") or {}
    status = svc.get("status") or {}
    lb = (status.get("loadBalancer") or status.get("load_balancer") or {})
    return {
        "namespace": m.get("namespace"),
        "name": m.get("name"),
        "type": spec.get("type"),
        "cluster_ip": spec.get("clusterIP") or spec.get("cluster_ip"),
        "external_ips": spec.get("externalIPs") or spec.get("external_ips"),
        "lb_ingress": [
            (i.get("ip") or i.get("hostname"))
            for i in (lb.get("ingress") or [])
        ],
        "ports": [
            {
                "name": p.get("name"),
                "protocol": p.get("protocol"),
                "port": p.get("port"),
                "target_port": p.get("targetPort") or p.get("target_port"),
                "node_port": p.get("nodePort") or p.get("node_port"),
            }
            for p in (spec.get("ports") or [])
        ],
        "selector": spec.get("selector"),
        "created_at": m.get("creationTimestamp") or m.get("creation_timestamp"),
    }


def _slim_event(ev: dict) -> dict:
    inv = ev.get("involvedObject") or ev.get("involved_object") or {}
    return {
        "namespace": (ev.get("metadata") or {}).get("namespace"),
        "type": ev.get("type"),
        "reason": ev.get("reason"),
        "message": ev.get("message"),
        "object_kind": inv.get("kind"),
        "object_name": inv.get("name"),
        "count": ev.get("count"),
        "first_seen": ev.get("firstTimestamp")
        or ev.get("first_timestamp")
        or ev.get("eventTime")
        or ev.get("event_time"),
        "last_seen": ev.get("lastTimestamp")
        or ev.get("last_timestamp")
        or ev.get("eventTime")
        or ev.get("event_time"),
        "source": (ev.get("source") or {}).get("component"),
    }


def _describe_pod_full(pod: dict) -> dict:
    """Build a 'describe'-style summary including resources and probes."""
    m = _meta(pod)
    spec = pod.get("spec") or {}
    status = pod.get("status") or {}
    containers_spec = spec.get("containers") or []
    container_statuses = (
        status.get("containerStatuses") or status.get("container_statuses") or []
    )

    # Index spec containers by name for resource/probe extraction
    spec_by_name = {c.get("name"): c for c in containers_spec if c.get("name")}

    def _probe_summary(p: Optional[dict]) -> Optional[dict]:
        if not p:
            return None
        return {
            "initial_delay": p.get("initialDelaySeconds")
            or p.get("initial_delay_seconds"),
            "period": p.get("periodSeconds") or p.get("period_seconds"),
            "timeout": p.get("timeoutSeconds") or p.get("timeout_seconds"),
            "failure_threshold": p.get("failureThreshold")
            or p.get("failure_threshold"),
            "success_threshold": p.get("successThreshold")
            or p.get("success_threshold"),
            "type": (
                "http" if p.get("httpGet") or p.get("http_get") else
                "tcp" if p.get("tcpSocket") or p.get("tcp_socket") else
                "exec" if p.get("exec") else
                "grpc" if p.get("grpc") else
                "unknown"
            ),
        }

    containers_view = []
    for cs in container_statuses:
        name = cs.get("name")
        spec_c = spec_by_name.get(name) or {}
        resources = spec_c.get("resources") or {}
        view = _container_status_summary(cs)
        view["resources"] = {
            "requests": resources.get("requests"),
            "limits": resources.get("limits"),
        }
        view["liveness_probe"] = _probe_summary(
            spec_c.get("livenessProbe") or spec_c.get("liveness_probe")
        )
        view["readiness_probe"] = _probe_summary(
            spec_c.get("readinessProbe") or spec_c.get("readiness_probe")
        )
        view["startup_probe"] = _probe_summary(
            spec_c.get("startupProbe") or spec_c.get("startup_probe")
        )
        containers_view.append(view)

    return {
        "namespace": m.get("namespace"),
        "name": m.get("name"),
        "phase": status.get("phase"),
        "node": spec.get("nodeName") or spec.get("node_name"),
        "pod_ip": status.get("podIP") or status.get("pod_ip"),
        "host_ip": status.get("hostIP") or status.get("host_ip"),
        "service_account": spec.get("serviceAccountName")
        or spec.get("service_account_name"),
        "qos_class": status.get("qosClass") or status.get("qos_class"),
        "start_time": status.get("startTime") or status.get("start_time"),
        "owner": _pod_owner(m),
        "labels": m.get("labels"),
        "annotations": m.get("annotations"),
        "conditions": [
            {
                "type": c.get("type"),
                "status": c.get("status"),
                "reason": c.get("reason"),
                "message": (c.get("message") or "")[:200] or None,
                "last_transition": c.get("lastTransitionTime")
                or c.get("last_transition_time"),
            }
            for c in (status.get("conditions") or [])
        ],
        "containers": containers_view,
        "init_containers": [
            _container_status_summary(cs)
            for cs in (
                status.get("initContainerStatuses")
                or status.get("init_container_statuses")
                or []
            )
        ],
        "total_restarts": sum(c.get("restart_count") or 0 for c in containers_view),
    }


def _rollout_status(dep: dict) -> dict:
    """Render Deployment status as a human-friendly rollout summary."""
    m = _meta(dep)
    spec = dep.get("spec") or {}
    status = dep.get("status") or {}
    desired = spec.get("replicas") or 0
    ready = status.get("readyReplicas") or status.get("ready_replicas") or 0
    updated = status.get("updatedReplicas") or status.get("updated_replicas") or 0
    available = status.get("availableReplicas") or status.get("available_replicas") or 0
    unavailable = (
        status.get("unavailableReplicas")
        or status.get("unavailable_replicas")
        or 0
    )
    observed = status.get("observedGeneration") or status.get("observed_generation")
    generation = m.get("generation")

    conditions = status.get("conditions") or []
    progressing = next(
        (c for c in conditions if c.get("type") == "Progressing"), None
    )
    available_cond = next(
        (c for c in conditions if c.get("type") == "Available"), None
    )

    if progressing and progressing.get("reason") == "ProgressDeadlineExceeded":
        state = "Failed"
    elif desired == ready == updated == available and unavailable == 0:
        state = "Complete"
    else:
        state = "Progressing"

    return {
        "namespace": m.get("namespace"),
        "name": m.get("name"),
        "state": state,
        "replicas_desired": desired,
        "replicas_ready": ready,
        "replicas_updated": updated,
        "replicas_available": available,
        "replicas_unavailable": unavailable,
        "observed_generation": observed,
        "generation": generation,
        "progressing": {
            "status": (progressing or {}).get("status"),
            "reason": (progressing or {}).get("reason"),
            "message": (progressing or {}).get("message"),
        } if progressing else None,
        "available": {
            "status": (available_cond or {}).get("status"),
            "reason": (available_cond or {}).get("reason"),
            "message": (available_cond or {}).get("message"),
        } if available_cond else None,
    }


def _slim_pod_metric(item: dict) -> dict:
    m = _meta(item)
    containers = item.get("containers") or []
    cpu_total = 0
    mem_total = 0
    parsed_cpu, parsed_mem = True, True
    for c in containers:
        usage = c.get("usage") or {}
        try:
            cpu_total += _cpu_to_millicores(usage.get("cpu") or "0")
        except Exception:
            parsed_cpu = False
        try:
            mem_total += _mem_to_bytes(usage.get("memory") or "0")
        except Exception:
            parsed_mem = False
    return {
        "namespace": m.get("namespace"),
        "name": m.get("name"),
        "cpu_millicores": cpu_total if parsed_cpu else None,
        "memory_bytes": mem_total if parsed_mem else None,
        "memory_mib": round(mem_total / (1024 * 1024), 1) if parsed_mem else None,
        "containers": [
            {
                "name": c.get("name"),
                "cpu": (c.get("usage") or {}).get("cpu"),
                "memory": (c.get("usage") or {}).get("memory"),
            }
            for c in containers
        ],
        "window": item.get("window"),
        "timestamp": item.get("timestamp"),
    }


def _slim_node_metric(item: dict) -> dict:
    m = _meta(item)
    usage = item.get("usage") or {}
    try:
        cpu_m = _cpu_to_millicores(usage.get("cpu") or "0")
    except Exception:
        cpu_m = None
    try:
        mem_b = _mem_to_bytes(usage.get("memory") or "0")
        mem_mib = round(mem_b / (1024 * 1024), 1)
    except Exception:
        mem_b, mem_mib = None, None
    return {
        "name": m.get("name"),
        "cpu_millicores": cpu_m,
        "memory_bytes": mem_b,
        "memory_mib": mem_mib,
        "window": item.get("window"),
        "timestamp": item.get("timestamp"),
    }


def _cpu_to_millicores(v: str) -> int:
    """Convert a Kubernetes CPU quantity (e.g. '100m', '1.5', '500u') to millicores."""
    s = str(v).strip()
    if not s:
        return 0
    if s.endswith("n"):
        return int(float(s[:-1]) / 1_000_000)
    if s.endswith("u"):
        return int(float(s[:-1]) / 1_000)
    if s.endswith("m"):
        return int(float(s[:-1]))
    return int(float(s) * 1000)


def _mem_to_bytes(v: str) -> int:
    """Convert a Kubernetes memory quantity (e.g. '128Mi', '1Gi') to bytes."""
    s = str(v).strip()
    if not s:
        return 0
    units = {
        "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
        "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4,
    }
    for suf, mult in units.items():
        if s.endswith(suf):
            return int(float(s[: -len(suf)]) * mult)
    return int(float(s))


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class K8sObserveTool(BaseTool):
    """Read-only inspection of a Kubernetes cluster.

    Use one ``action`` per call. ``namespace`` defaults to the configured
    ``default_namespace`` when omitted (some actions accept all-namespaces
    by leaving it unset; see per-action docs).

    Tabular results auto-export as CSV when over 100 rows; the agent
    receives only the first 100 rows plus a download link.

    Permission: ``k8s:read``.
    """

    name: ClassVar[str] = "k8s_observe"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Read-only Kubernetes inspection: namespaces, deployments, pods, "
        "logs, events, rollout status, resource usage. Auto-CSV on >100 rows."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "kubernetes"
    permissions: ClassVar[list[str]] = ["k8s:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "target_entities": "name",
        "target_resource": "namespace",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which read operation to perform.",
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
                "description": (
                    "Namespace. Required for describe_pod, get_logs, "
                    "get_rollout_status. Optional for list_pods / "
                    "list_deployments / list_services / get_events / top_pods "
                    "(omit for all-namespaces). Ignored for list_namespaces / "
                    "list_nodes / top_nodes."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "[describe_pod, get_logs] Pod name. "
                    "[get_rollout_status] Deployment name."
                ),
            },
            "label_selector": {
                "type": "string",
                "description": (
                    "[list_pods, list_deployments, list_services] Label "
                    "selector (e.g. 'app=nginx,tier=frontend')."
                ),
            },
            "field_selector": {
                "type": "string",
                "description": (
                    "[list_pods, get_events] Field selector "
                    "(e.g. 'status.phase=Running')."
                ),
            },
            "status_phase": {
                "type": "string",
                "enum": ["Pending", "Running", "Succeeded", "Failed", "Unknown"],
                "description": (
                    "[list_pods] Convenience filter for pod phase. Combined "
                    "with field_selector when both are given."
                ),
            },
            "container": {
                "type": "string",
                "description": "[get_logs] Container name (defaults to first container).",
            },
            "tail_lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": _LOG_TAIL_MAX,
                "description": (
                    f"[get_logs] Last N lines (default {_LOG_TAIL_DEFAULT}, "
                    f"max {_LOG_TAIL_MAX})."
                ),
            },
            "since_seconds": {
                "type": "integer",
                "minimum": 1,
                "description": "[get_logs] Only logs newer than N seconds.",
            },
            "previous": {
                "type": "boolean",
                "description": (
                    "[get_logs] Fetch logs of the previous container "
                    "instance (e.g. last crash). Default false."
                ),
            },
            "timestamps": {
                "type": "boolean",
                "description": "[get_logs] Prepend RFC3339 timestamps to each line.",
            },
            "type_filter": {
                "type": "string",
                "enum": ["Normal", "Warning", "All"],
                "description": "[get_events] Filter by event type (default: All).",
            },
            "involved_object_kind": {
                "type": "string",
                "description": (
                    "[get_events] Filter by involvedObject.kind "
                    "(e.g. 'Pod', 'Deployment')."
                ),
            },
            "involved_object_name": {
                "type": "string",
                "description": "[get_events] Filter by involvedObject.name.",
            },
            "sort_by": {
                "type": "string",
                "enum": ["cpu", "memory"],
                "description": "[top_pods, top_nodes] Sort key (default: cpu).",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_RESULTS,
                "description": (
                    f"[list_*, get_events, top_*] Maximum items to return "
                    f"(hard cap {_MAX_RESULTS}, default {_DEFAULT_RESULTS})."
                ),
            },
            "export_csv": {
                "type": "boolean",
                "description": (
                    "[list_*, get_events, top_*] Force CSV artifact even for "
                    "small results. CSV is generated automatically when the "
                    f"result exceeds {AGENT_PREVIEW_ROWS} rows regardless."
                ),
            },
            "export_json": {
                "type": "boolean",
                "description": (
                    "[list_*, get_events, top_*] Persist the full result as a "
                    "JSON artifact. Use only when programmatic post-processing "
                    "is needed; otherwise prefer 'export_csv'."
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

    # ── Execute ──────────────────────────────────────────────────────────────

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

        max_results = min(
            int(params.get("max_results") or _DEFAULT_RESULTS), _MAX_RESULTS
        )

        try:
            async with build_k8s_client(config) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(client, params, agent_context, max_results)
        except K8sError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(exc.code, str(exc), execution_time_ms=elapsed)
        except _ParamError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure("INVALID_PARAMS", str(exc),
                                 execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("k8s_observe(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={"action": action, **data}, execution_time_ms=elapsed
        )

    # ── Tabular helper ──────────────────────────────────────────────────────

    async def _tabular(
        self,
        agent_context: AgentContext,
        action: str,
        rows: list[dict],
        params: dict,
        *,
        filename_suffix: str = "",
    ) -> dict:
        """Apply the result_export pipeline to a tabular result."""
        suffix = f"_{filename_suffix}" if filename_suffix else ""
        agent_payload = await build_agent_payload(
            tool=self,
            rows=rows,
            export_csv=bool(params.get("export_csv", False)),
            export_json=bool(params.get("export_json", False)),
            filename_prefix=f"{self.name}_{action}{suffix}",
            agent_context=agent_context,
        )
        return {
            "rows_total": agent_payload["rows_total"],
            "rows_overflow": agent_payload["rows_overflow"],
            "rows_preview_limit": AGENT_PREVIEW_ROWS,
            "artifacts": agent_payload["artifacts"],
            "agent_hint": agent_payload["agent_hint"],
            "rows": agent_payload["rows_preview"],
        }

    # ── Action handlers ─────────────────────────────────────────────────────

    async def _do_list_namespaces(
        self,
        client: K8sClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        items = await client.list_namespaces()
        rows = [_slim_namespace(ns) for ns in items[:max_results]]
        truncated = len(items) > max_results
        out = await self._tabular(agent_context, "list_namespaces", rows, params)
        out["truncated"] = truncated
        return out

    async def _do_list_nodes(
        self,
        client: K8sClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        items = await client.list_nodes()
        rows = [_slim_node(n) for n in items[:max_results]]
        truncated = len(items) > max_results
        out = await self._tabular(agent_context, "list_nodes", rows, params)
        out["truncated"] = truncated
        return out

    async def _do_list_services(
        self,
        client: K8sClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        ns = params.get("namespace")
        items = await client.list_services(
            namespace=ns,
            label_selector=params.get("label_selector"),
            limit=max_results,
        )
        rows = [_slim_service(s) for s in items[:max_results]]
        truncated = len(items) > max_results
        out = await self._tabular(agent_context, "list_services", rows, params)
        out["namespace"] = ns
        out["truncated"] = truncated
        return out

    async def _do_list_deployments(
        self,
        client: K8sClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        ns = params.get("namespace")
        items = await client.list_deployments(
            namespace=ns,
            label_selector=params.get("label_selector"),
            limit=max_results,
        )
        rows = [_slim_deployment(d) for d in items[:max_results]]
        truncated = len(items) > max_results
        out = await self._tabular(agent_context, "list_deployments", rows, params)
        out["namespace"] = ns
        out["truncated"] = truncated
        return out

    async def _do_list_pods(
        self,
        client: K8sClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        ns = params.get("namespace")
        field_selector = params.get("field_selector")
        if phase := params.get("status_phase"):
            phase_sel = f"status.phase={phase}"
            field_selector = (
                f"{field_selector},{phase_sel}" if field_selector else phase_sel
            )

        items = await client.list_pods(
            namespace=ns,
            label_selector=params.get("label_selector"),
            field_selector=field_selector,
            limit=max_results,
        )
        rows = [_slim_pod(p) for p in items[:max_results]]
        truncated = len(items) > max_results
        out = await self._tabular(agent_context, "list_pods", rows, params)
        out["namespace"] = ns
        out["truncated"] = truncated
        return out

    async def _do_describe_pod(
        self,
        client: K8sClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        ns = _require_namespace(params, client)
        name = _require(params, "name")
        pod = await client.read_pod(ns, name)
        # Fetch related events (best-effort)
        events: list[dict] = []
        try:
            ev_items = await client.list_events(
                namespace=ns,
                field_selector=(
                    f"involvedObject.name={name},involvedObject.kind=Pod"
                ),
                limit=50,
            )
            events = [_slim_event(e) for e in ev_items]
        except K8sError as exc:
            log.debug("describe_pod: events fetch failed: %s", exc)
        return {
            "namespace": ns,
            "name": name,
            "pod": _describe_pod_full(pod),
            "events": events,
        }

    async def _do_get_logs(
        self,
        client: K8sClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        ns = _require_namespace(params, client)
        name = _require(params, "name")
        tail = min(
            int(params.get("tail_lines") or _LOG_TAIL_DEFAULT), _LOG_TAIL_MAX
        )
        logs = await client.read_pod_log(
            ns,
            name,
            container=params.get("container"),
            tail_lines=tail,
            since_seconds=params.get("since_seconds"),
            previous=bool(params.get("previous", False)),
            timestamps=bool(params.get("timestamps", False)),
            limit_bytes=_LOG_BYTES_LIMIT,
        )
        text = logs or ""
        lines = text.splitlines()
        return {
            "namespace": ns,
            "name": name,
            "container": params.get("container"),
            "tail_lines_requested": tail,
            "lines_returned": len(lines),
            "bytes_returned": len(text.encode("utf-8")),
            "previous": bool(params.get("previous", False)),
            "logs": text,
        }

    async def _do_get_events(
        self,
        client: K8sClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        ns = params.get("namespace")
        type_filter = params.get("type_filter") or "All"

        selectors: list[str] = []
        if type_filter in ("Normal", "Warning"):
            selectors.append(f"type={type_filter}")
        if kind := params.get("involved_object_kind"):
            selectors.append(f"involvedObject.kind={kind}")
        if obj_name := params.get("involved_object_name"):
            selectors.append(f"involvedObject.name={obj_name}")
        field_selector = ",".join(selectors) if selectors else None

        items = await client.list_events(
            namespace=ns, field_selector=field_selector, limit=max_results
        )
        # Sort newest first
        items.sort(
            key=lambda e: (
                e.get("lastTimestamp")
                or e.get("last_timestamp")
                or e.get("eventTime")
                or e.get("event_time")
                or ""
            ),
            reverse=True,
        )
        rows = [_slim_event(e) for e in items[:max_results]]
        truncated = len(items) > max_results
        out = await self._tabular(agent_context, "get_events", rows, params)
        out["namespace"] = ns
        out["type_filter"] = type_filter
        out["truncated"] = truncated
        return out

    async def _do_get_rollout_status(
        self,
        client: K8sClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        ns = _require_namespace(params, client)
        name = _require(params, "name")
        dep = await client.read_deployment(ns, name)
        return {"rollout": _rollout_status(dep)}

    async def _do_top_pods(
        self,
        client: K8sClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        ns = params.get("namespace")
        sort_by = params.get("sort_by") or "cpu"
        try:
            items = await client.top_pods(namespace=ns)
        except K8sError as exc:
            if exc.status_code == 404:
                raise K8sError(
                    "metrics.k8s.io API not available — install metrics-server "
                    "to enable 'top_pods'.",
                    code="UPSTREAM_ERROR",
                    status_code=404,
                ) from exc
            raise

        rows = [_slim_pod_metric(i) for i in items]
        key = "cpu_millicores" if sort_by == "cpu" else "memory_bytes"
        rows.sort(key=lambda r: (r.get(key) or 0), reverse=True)
        rows = rows[:max_results]

        out = await self._tabular(agent_context, "top_pods", rows, params)
        out["namespace"] = ns
        out["sort_by"] = sort_by
        return out

    async def _do_top_nodes(
        self,
        client: K8sClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        sort_by = params.get("sort_by") or "cpu"
        try:
            items = await client.top_nodes()
        except K8sError as exc:
            if exc.status_code == 404:
                raise K8sError(
                    "metrics.k8s.io API not available — install metrics-server "
                    "to enable 'top_nodes'.",
                    code="UPSTREAM_ERROR",
                    status_code=404,
                ) from exc
            raise

        rows = [_slim_node_metric(i) for i in items]
        key = "cpu_millicores" if sort_by == "cpu" else "memory_bytes"
        rows.sort(key=lambda r: (r.get(key) or 0), reverse=True)
        rows = rows[:max_results]

        out = await self._tabular(agent_context, "top_nodes", rows, params)
        out["sort_by"] = sort_by
        return out


# ---------------------------------------------------------------------------
# Param helpers
# ---------------------------------------------------------------------------


class _ParamError(Exception):
    pass


def _require(params: dict, field: str) -> str:
    val = (params.get(field) or "").strip() if isinstance(params.get(field), str) \
        else params.get(field)
    if not val:
        raise _ParamError(f"'{field}' is required for this action.")
    return str(val)


def _require_namespace(params: dict, client: K8sClient) -> str:
    ns = (params.get("namespace") or "").strip()
    if not ns:
        ns = client.default_namespace
    if not ns:
        raise _ParamError("'namespace' is required for this action.")
    return ns


# Silence unused-imports flagged by very strict checkers
_ = Any
