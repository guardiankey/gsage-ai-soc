"""gSage AI — Kubernetes high-level dashboards.

Aggregates the read-only primitives exposed by ``k8s_observe`` into a
small set of curated views Tier 1/2 analysts use during the first 60
seconds of an incident:

- ``cluster_overview``: nodes ready / not-ready, namespaces, pod phase
  totals across the whole cluster.
- ``workload_health``: deployments with replicas_ready < replicas_desired
  in a given namespace (or all).
- ``top_restarts``: pods sorted by restart count.
- ``pending_pods``: pods stuck in Pending with their ``status.message``.
- ``recent_warnings``: latest Warning events (cluster-wide or per ns).
- ``resource_pressure``: top N pods/nodes by CPU and memory usage
  (requires metrics-server; gracefully degrades when missing).
- ``namespace_overview``: a per-namespace digest (workloads, pods,
  warnings).

All views are bounded — at most a few hundred items per upstream call —
and return a structured ``data`` payload meant to be rendered as cards
or summarised by the agent.

Permission: ``k8s:read``.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
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

_VIEWS = frozenset({
    "cluster_overview",
    "workload_health",
    "top_restarts",
    "pending_pods",
    "recent_warnings",
    "resource_pressure",
    "namespace_overview",
})

_FETCH_CAP = 500
_DEFAULT_TOP_N = 10


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class K8sDashboardTool(BaseTool):
    """High-level Kubernetes dashboards aggregating read-only primitives.

    Each ``view`` returns a structured payload tailored to a single
    investigative question (e.g. "what's breaking?", "where is the load?").

    Permission: ``k8s:read``.
    """

    name: ClassVar[str] = "k8s_dashboard"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Curated Kubernetes dashboards: cluster overview, workload health, "
        "top restarts, pending pods, recent warnings, resource pressure, "
        "namespace overview."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "kubernetes"
    permissions: ClassVar[list[str]] = ["k8s:read"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 90
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "view",
        "target_resource": "namespace",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["view"],
        "properties": {
            "view": {
                "type": "string",
                "enum": sorted(_VIEWS),
                "description": "Which dashboard view to render.",
            },
            "profile": {
                "type": "string",
                "description": (
                    "GSageToolConfig profile (cluster) to use. Omit for "
                    "'default'."
                ),
            },
            "namespace": {
                "type": "string",
                "description": (
                    "Namespace to scope the view to. Optional for "
                    "workload_health, top_restarts, pending_pods, "
                    "recent_warnings, resource_pressure (omit = whole "
                    "cluster). Required for namespace_overview."
                ),
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": (
                    "[top_restarts, resource_pressure] Number of items to "
                    f"include (default {_DEFAULT_TOP_N})."
                ),
            },
            "since_minutes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1440,
                "description": (
                    "[recent_warnings] Look back window in minutes (default 60)."
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
        view = (params.get("view") or "").strip()
        if view not in _VIEWS:
            return self._failure(
                "INVALID_PARAMS",
                f"view must be one of {sorted(_VIEWS)}; got {view!r}.",
            )

        try:
            async with build_k8s_client(config) as client:
                handler = getattr(self, f"_view_{view}")
                data = await handler(client, params)
        except K8sError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(exc.code, str(exc), execution_time_ms=elapsed)
        except Exception as exc:
            log.exception("k8s_dashboard(%s): unexpected error", view)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={
                "view": view,
                "namespace": params.get("namespace"),
                "generated_at": _now_iso(),
                **data,
            },
            execution_time_ms=elapsed,
        )

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _node_ready(node: dict) -> Optional[bool]:
        for c in (node.get("status") or {}).get("conditions") or []:
            if c.get("type") == "Ready":
                return c.get("status") == "True"
        return None

    @staticmethod
    def _pod_restarts(pod: dict) -> int:
        cs = (pod.get("status") or {}).get("containerStatuses") or (
            pod.get("status") or {}
        ).get("container_statuses") or []
        return sum(
            int(c.get("restartCount") or c.get("restart_count") or 0)
            for c in cs
        )

    @staticmethod
    def _pod_meta(pod: dict) -> tuple[str, str]:
        m = pod.get("metadata") or {}
        return (m.get("namespace") or "", m.get("name") or "")

    # ── Views ───────────────────────────────────────────────────────────────

    async def _view_cluster_overview(
        self, client: K8sClient, params: dict
    ) -> dict:
        nodes = await client.list_nodes()
        namespaces = await client.list_namespaces()
        pods = await client.list_pods(limit=_FETCH_CAP)
        deployments = await client.list_deployments(limit=_FETCH_CAP)

        nodes_ready = sum(1 for n in nodes if self._node_ready(n) is True)
        nodes_not_ready = sum(
            1 for n in nodes if self._node_ready(n) is False
        )
        nodes_unknown = sum(
            1 for n in nodes if self._node_ready(n) is None
        )

        phase_counter: Counter[str] = Counter(
            (p.get("status") or {}).get("phase") or "Unknown" for p in pods
        )

        # Quick deployment health
        unhealthy_deps = 0
        for d in deployments:
            spec = d.get("spec") or {}
            status = d.get("status") or {}
            desired = spec.get("replicas") or 0
            ready = (
                status.get("readyReplicas")
                or status.get("ready_replicas")
                or 0
            )
            if desired and ready < desired:
                unhealthy_deps += 1

        return {
            "data": {
                "nodes": {
                    "total": len(nodes),
                    "ready": nodes_ready,
                    "not_ready": nodes_not_ready,
                    "unknown": nodes_unknown,
                },
                "namespaces": {"total": len(namespaces)},
                "pods": {
                    "total": len(pods),
                    "by_phase": dict(phase_counter),
                    "fetched_capped_at": _FETCH_CAP,
                },
                "deployments": {
                    "total": len(deployments),
                    "unhealthy": unhealthy_deps,
                    "fetched_capped_at": _FETCH_CAP,
                },
            },
            "fetched": {
                "nodes": len(nodes),
                "namespaces": len(namespaces),
                "pods": len(pods),
                "deployments": len(deployments),
            },
        }

    async def _view_workload_health(
        self, client: K8sClient, params: dict
    ) -> dict:
        ns = params.get("namespace")
        deployments = await client.list_deployments(
            namespace=ns, limit=_FETCH_CAP
        )
        unhealthy = []
        for d in deployments:
            m = d.get("metadata") or {}
            spec = d.get("spec") or {}
            status = d.get("status") or {}
            desired = spec.get("replicas") or 0
            ready = (
                status.get("readyReplicas")
                or status.get("ready_replicas")
                or 0
            )
            available = (
                status.get("availableReplicas")
                or status.get("available_replicas")
                or 0
            )
            unavailable = (
                status.get("unavailableReplicas")
                or status.get("unavailable_replicas")
                or 0
            )
            if desired and (ready < desired or unavailable):
                conditions = status.get("conditions") or []
                progressing = next(
                    (c for c in conditions if c.get("type") == "Progressing"),
                    None,
                )
                unhealthy.append({
                    "namespace": m.get("namespace"),
                    "name": m.get("name"),
                    "replicas_desired": desired,
                    "replicas_ready": ready,
                    "replicas_available": available,
                    "replicas_unavailable": unavailable,
                    "progressing_reason": (progressing or {}).get("reason"),
                    "progressing_message": (progressing or {}).get("message"),
                })

        unhealthy.sort(
            key=lambda r: (r["replicas_desired"] - r["replicas_ready"]),
            reverse=True,
        )

        return {
            "data": {
                "deployments_total": len(deployments),
                "deployments_unhealthy": len(unhealthy),
                "items": unhealthy,
            },
            "fetched": {"deployments": len(deployments)},
        }

    async def _view_top_restarts(
        self, client: K8sClient, params: dict
    ) -> dict:
        ns = params.get("namespace")
        top_n = int(params.get("top_n") or _DEFAULT_TOP_N)
        pods = await client.list_pods(namespace=ns, limit=_FETCH_CAP)
        rows = []
        for p in pods:
            n_restarts = self._pod_restarts(p)
            if n_restarts <= 0:
                continue
            ns_p, name_p = self._pod_meta(p)
            cs_list = (p.get("status") or {}).get("containerStatuses") or (
                p.get("status") or {}
            ).get("container_statuses") or []
            worst = max(
                cs_list,
                key=lambda c: int(
                    c.get("restartCount") or c.get("restart_count") or 0
                ),
                default={},
            )
            last_state = worst.get("lastState") or worst.get("last_state") or {}
            last_term = last_state.get("terminated") or {}
            rows.append({
                "namespace": ns_p,
                "name": name_p,
                "restart_count": n_restarts,
                "phase": (p.get("status") or {}).get("phase"),
                "worst_container": worst.get("name"),
                "last_terminated_reason": last_term.get("reason"),
                "last_terminated_exit_code": last_term.get("exitCode")
                or last_term.get("exit_code"),
            })

        rows.sort(key=lambda r: r["restart_count"], reverse=True)
        rows = rows[:top_n]
        return {
            "data": {"items": rows, "top_n": top_n},
            "fetched": {"pods": len(pods)},
        }

    async def _view_pending_pods(
        self, client: K8sClient, params: dict
    ) -> dict:
        ns = params.get("namespace")
        pods = await client.list_pods(
            namespace=ns,
            field_selector="status.phase=Pending",
            limit=_FETCH_CAP,
        )
        rows = []
        for p in pods:
            ns_p, name_p = self._pod_meta(p)
            status = p.get("status") or {}
            conditions = status.get("conditions") or []
            scheduled = next(
                (c for c in conditions if c.get("type") == "PodScheduled"),
                None,
            )
            rows.append({
                "namespace": ns_p,
                "name": name_p,
                "phase": status.get("phase"),
                "reason": status.get("reason"),
                "message": (status.get("message") or "")[:300] or None,
                "scheduled": (scheduled or {}).get("status") == "True"
                if scheduled else None,
                "schedule_reason": (scheduled or {}).get("reason"),
                "schedule_message": ((scheduled or {}).get("message") or "")[:300]
                or None,
                "created_at": (p.get("metadata") or {}).get("creationTimestamp")
                or (p.get("metadata") or {}).get("creation_timestamp"),
            })
        return {
            "data": {"items": rows, "count": len(rows)},
            "fetched": {"pods": len(pods)},
        }

    async def _view_recent_warnings(
        self, client: K8sClient, params: dict
    ) -> dict:
        ns = params.get("namespace")
        since_minutes = int(params.get("since_minutes") or 60)
        cutoff = datetime.now(timezone.utc).timestamp() - since_minutes * 60

        items = await client.list_events(
            namespace=ns,
            field_selector="type=Warning",
            limit=_FETCH_CAP,
        )

        def _ts(ev: dict) -> Optional[float]:
            for k in ("lastTimestamp", "last_timestamp", "eventTime",
                      "event_time", "firstTimestamp", "first_timestamp"):
                v = ev.get(k)
                if v:
                    try:
                        return datetime.fromisoformat(
                            str(v).replace("Z", "+00:00")
                        ).timestamp()
                    except Exception:
                        pass
            return None

        rows = []
        for ev in items:
            ts = _ts(ev)
            if ts is not None and ts < cutoff:
                continue
            inv = ev.get("involvedObject") or ev.get("involved_object") or {}
            rows.append({
                "namespace": (ev.get("metadata") or {}).get("namespace"),
                "reason": ev.get("reason"),
                "object_kind": inv.get("kind"),
                "object_name": inv.get("name"),
                "message": (ev.get("message") or "")[:400],
                "count": ev.get("count"),
                "last_seen": ev.get("lastTimestamp")
                or ev.get("last_timestamp")
                or ev.get("eventTime")
                or ev.get("event_time"),
            })

        rows.sort(key=lambda r: r.get("last_seen") or "", reverse=True)

        # Group by reason for a summary
        by_reason = Counter(r["reason"] or "unknown" for r in rows)

        return {
            "data": {
                "since_minutes": since_minutes,
                "items": rows,
                "count": len(rows),
                "by_reason": dict(by_reason.most_common()),
            },
            "fetched": {"events": len(items)},
        }

    async def _view_resource_pressure(
        self, client: K8sClient, params: dict
    ) -> dict:
        ns = params.get("namespace")
        top_n = int(params.get("top_n") or _DEFAULT_TOP_N)

        pods_avail = True
        nodes_avail = True
        try:
            pod_metrics = await client.top_pods(namespace=ns)
        except K8sError as exc:
            if exc.status_code == 404:
                pod_metrics = []
                pods_avail = False
            else:
                raise
        try:
            node_metrics = await client.top_nodes()
        except K8sError as exc:
            if exc.status_code == 404:
                node_metrics = []
                nodes_avail = False
            else:
                raise

        from src.mcp_server.tools.devops.k8s.k8s_observe import (  # noqa: PLC0415
            _slim_node_metric,
            _slim_pod_metric,
        )

        pod_rows = [_slim_pod_metric(i) for i in pod_metrics]
        node_rows = [_slim_node_metric(i) for i in node_metrics]

        top_pods_cpu = sorted(
            pod_rows, key=lambda r: r.get("cpu_millicores") or 0, reverse=True
        )[:top_n]
        top_pods_mem = sorted(
            pod_rows, key=lambda r: r.get("memory_bytes") or 0, reverse=True
        )[:top_n]
        top_nodes_cpu = sorted(
            node_rows, key=lambda r: r.get("cpu_millicores") or 0, reverse=True
        )[:top_n]
        top_nodes_mem = sorted(
            node_rows, key=lambda r: r.get("memory_bytes") or 0, reverse=True
        )[:top_n]

        return {
            "data": {
                "metrics_server_available": pods_avail and nodes_avail,
                "top_pods_cpu": top_pods_cpu,
                "top_pods_memory": top_pods_mem,
                "top_nodes_cpu": top_nodes_cpu,
                "top_nodes_memory": top_nodes_mem,
            },
            "fetched": {
                "pod_metrics": len(pod_rows),
                "node_metrics": len(node_rows),
            },
        }

    async def _view_namespace_overview(
        self, client: K8sClient, params: dict
    ) -> dict:
        ns = (params.get("namespace") or "").strip()
        if not ns:
            raise K8sError(
                "'namespace' is required for view=namespace_overview.",
                code="INVALID_PARAMS",
            )

        deployments = await client.list_deployments(
            namespace=ns, limit=_FETCH_CAP
        )
        pods = await client.list_pods(namespace=ns, limit=_FETCH_CAP)
        services = await client.list_services(namespace=ns, limit=_FETCH_CAP)
        events = await client.list_events(
            namespace=ns,
            field_selector="type=Warning",
            limit=200,
        )

        phase_counter: Counter[str] = Counter(
            (p.get("status") or {}).get("phase") or "Unknown" for p in pods
        )
        unhealthy_deps = 0
        for d in deployments:
            spec = d.get("spec") or {}
            status = d.get("status") or {}
            desired = spec.get("replicas") or 0
            ready = (
                status.get("readyReplicas")
                or status.get("ready_replicas")
                or 0
            )
            if desired and ready < desired:
                unhealthy_deps += 1

        total_restarts = sum(self._pod_restarts(p) for p in pods)
        warning_reasons = Counter(
            ev.get("reason") or "unknown" for ev in events
        )

        return {
            "data": {
                "deployments": {
                    "total": len(deployments),
                    "unhealthy": unhealthy_deps,
                },
                "pods": {
                    "total": len(pods),
                    "by_phase": dict(phase_counter),
                    "total_restarts": total_restarts,
                },
                "services": {"total": len(services)},
                "warnings": {
                    "total": len(events),
                    "by_reason": dict(warning_reasons.most_common(10)),
                },
            },
            "fetched": {
                "deployments": len(deployments),
                "pods": len(pods),
                "services": len(services),
                "events": len(events),
            },
        }


_ = Any
