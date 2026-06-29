"""gSage AI — High-level VMware vCenter dashboards (read-only summaries).

Composes the lower-level inventory primitives (clusters, hosts, VMs,
datastores) into opinionated, ops-friendly summaries. Each view returns a
JSON object designed to be rendered as a dashboard tile — the vSphere
counterpart of :mod:`proxmox_dashboard` / :mod:`azure_dashboard`.

Available views:

- ``cluster_overview``    — Cluster / host / VM counts, DRS-HA posture and
                          aggregate compute capacity vs usage.
- ``host_health``          — Per-host CPU / memory usage, VM counts and the
                          busiest / unhealthy (disconnected / maintenance /
                          near-full) hosts.
- ``vm_health``            — Power-state and VMware Tools distribution, top
                          CPU / memory consumers, and VMs whose tools are
                          not running.
- ``datastore_summary``    — Datastore capacity / usage, the fullest stores
                          and a >85%% near-full alert.
- ``capacity_report``      — vCPU / memory overcommit (allocated vs physical)
                          and reclaimable waste (powered-off VMs, templates).

Inventory walks and property access run inside worker threads (pyVmomi is
synchronous). Permission: ``vcenter:read``. Cache TTL 300s.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.devops.vmware._cache import (
    CACHE_TTL_SECONDS,
    build_cache_key,
    cache_get,
    cache_set,
)
from src.mcp_server.tools.devops.vmware._client import (
    VCENTER_CONFIG_DEFAULTS,
    VCENTER_CONFIG_SCHEMA,
    VCenterClient,
    VCenterError,
    build_vcenter_client,
)
from src.mcp_server.tools.devops.vmware import _views as V
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_VIEWS = frozenset({
    "cluster_overview",
    "host_health",
    "vm_health",
    "datastore_summary",
    "capacity_report",
})

_NEAR_FULL_PCT = 85.0


def _sum(values: Any) -> float:
    return float(sum(v for v in values if isinstance(v, (int, float))))


def _gb_bytes(num_bytes: Any) -> Optional[float]:
    try:
        return round(float(num_bytes) / (1024 ** 3), 2)
    except (TypeError, ValueError):
        return None


def _host_cpu_capacity_mhz(h: dict) -> float:
    cores = h.get("cpu_cores") or 0
    per = h.get("cpu_mhz_per_core") or 0
    try:
        return float(cores) * float(per)
    except (TypeError, ValueError):
        return 0.0


class VCenterDashboardTool(BaseTool):
    """Aggregated read-only VMware vCenter dashboards.

    Use ``view`` to select a tile. Heavy inventory walks are cached for 5
    minutes per (org, user, profile, vcenter host, view).

    Permission: ``vcenter:read``.
    """

    name: ClassVar[str] = "vcenter_dashboard"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "VMware vCenter dashboards: cluster overview, host health, VM "
        "health, datastore summary, capacity/overcommit report."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "vcenter"
    permissions: ClassVar[list[str]] = ["vcenter:read"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 180
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "view",
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
                    "Name of the configured vCenter to summarize (a key "
                    "under the config 'profiles' map). Omit (or 'default') "
                    "for the primary vCenter."
                ),
            },
            "force_refresh": {
                "type": "boolean",
                "description": (
                    f"Bypass the Redis cache (TTL {CACHE_TTL_SECONDS}s)."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = VCENTER_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = VCENTER_CONFIG_DEFAULTS
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
            async with build_vcenter_client(
                config, profile=params.get("profile")
            ) as client:
                handler = getattr(self, f"_view_{view}")
                data = await handler(client, params, agent_context)
        except VCenterError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.code in ("CONNECTION_ERROR", "TIMEOUT")
            return self._failure(
                exc.code, str(exc), retryable=retryable,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("vcenter_dashboard(%s): unexpected error", view)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={"view": view, **data}, execution_time_ms=elapsed
        )

    # ── Cache ──────────────────────────────────────────────────────────────

    async def _cached(
        self,
        agent_context: AgentContext,
        client: VCenterClient,
        params: dict,
        view: str,
    ) -> tuple[Optional[dict], str]:
        org = str(getattr(agent_context, "org_id", "") or "")
        user = str(getattr(agent_context, "user_id", "") or "")
        profile = str(params.get("profile") or "default")
        key = build_cache_key(
            org_id=org, user_id=user, profile_id=profile,
            vcenter_host=client.host, kind=f"dashboard:{view}", filters={},
        )
        if params.get("force_refresh"):
            return None, key
        cached = await cache_get(key)
        if isinstance(cached, dict):
            return cached, key
        return None, key

    # ── Shared fetch helpers (slim built inside a worker thread) ─────────────

    async def _clusters(self, client: VCenterClient) -> list[dict]:
        objs = await client.list_objs("ClusterComputeResource")
        return await client.call(lambda: [V.slim_cluster(o) for o in objs])

    async def _hosts(self, client: VCenterClient) -> list[dict]:
        objs = await client.list_objs("HostSystem")
        return await client.call(lambda: [V.slim_host(o) for o in objs])

    async def _vms(self, client: VCenterClient) -> list[dict]:
        objs = await client.list_objs("VirtualMachine")
        return await client.call(lambda: [V.slim_vm(o) for o in objs])

    async def _datastores(self, client: VCenterClient) -> list[dict]:
        objs = await client.list_objs("Datastore")
        return await client.call(lambda: [V.slim_datastore(o) for o in objs])

    # ── Views ──────────────────────────────────────────────────────────────

    async def _view_cluster_overview(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        cached, key = await self._cached(
            agent_context, client, params, "cluster_overview"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        clusters = await self._clusters(client)
        hosts = await self._hosts(client)
        vms = await self._vms(client)
        non_templates = [v for v in vms if not v.get("is_template")]
        connected = [h for h in hosts if h.get("connection_state") == "connected"]

        counts = {
            "clusters": len(clusters),
            "hosts_total": len(hosts),
            "hosts_connected": len(connected),
            "hosts_in_maintenance": sum(1 for h in hosts if h.get("in_maintenance")),
            "vms_total": len(non_templates),
            "vms_powered_on": sum(
                1 for v in non_templates if v.get("power_state") == "poweredOn"
            ),
            "vms_powered_off": sum(
                1 for v in non_templates if v.get("power_state") != "poweredOn"
            ),
            "templates": sum(1 for v in vms if v.get("is_template")),
            "drs_enabled_clusters": sum(1 for c in clusters if c.get("drs_enabled")),
            "ha_enabled_clusters": sum(1 for c in clusters if c.get("ha_enabled")),
        }
        total_cpu_mhz = _sum(_host_cpu_capacity_mhz(h) for h in connected)
        used_cpu_mhz = _sum(h.get("cpu_usage_mhz") for h in connected)
        total_mem_gb = _sum(_gb_bytes(h.get("memory_bytes")) for h in connected)
        used_mem_gb = round(_sum(h.get("memory_usage_mb") for h in connected) / 1024, 2)
        capacity = {
            "total_cpu_mhz": total_cpu_mhz,
            "used_cpu_mhz": used_cpu_mhz,
            "cpu_used_percent": (
                round(used_cpu_mhz / total_cpu_mhz * 100, 1)
                if total_cpu_mhz else None
            ),
            "total_memory_gb": round(total_mem_gb, 2),
            "used_memory_gb": used_mem_gb,
            "memory_used_percent": (
                round(used_mem_gb / total_mem_gb * 100, 1) if total_mem_gb else None
            ),
        }
        result = {
            "counts": counts,
            "capacity": capacity,
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        await cache_set(key, result)
        return result

    async def _view_host_health(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        cached, key = await self._cached(
            agent_context, client, params, "host_health"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        hosts = await self._hosts(client)
        vms = await self._vms(client)
        vms_by_host: Counter = Counter(
            v.get("host") for v in vms if not v.get("is_template")
        )

        rows: list[dict] = []
        for h in hosts:
            cpu_cap = _host_cpu_capacity_mhz(h)
            cpu_used = h.get("cpu_usage_mhz") or 0
            mem_total_mb = (h.get("memory_bytes") or 0) / (1024 ** 2)
            mem_used_mb = h.get("memory_usage_mb") or 0
            rows.append({
                "name": h.get("name"),
                "cluster": h.get("cluster"),
                "connection_state": h.get("connection_state"),
                "in_maintenance": h.get("in_maintenance"),
                "cpu_used_percent": (
                    round(cpu_used / cpu_cap * 100, 1) if cpu_cap else None
                ),
                "memory_used_percent": (
                    round(mem_used_mb / mem_total_mb * 100, 1) if mem_total_mb else None
                ),
                "memory_total_gb": _gb_bytes(h.get("memory_bytes")),
                "vm_count": vms_by_host.get(h.get("name"), 0),
                "uptime_seconds": h.get("uptime_seconds"),
                "overall_status": h.get("overall_status"),
            })

        unhealthy = [
            r for r in rows
            if r.get("connection_state") != "connected"
            or r.get("in_maintenance")
            or (r.get("memory_used_percent") or 0) >= _NEAR_FULL_PCT
            or r.get("overall_status") in ("red", "yellow")
        ]
        result = {
            "host_count": len(rows),
            "hosts": rows,
            "top_cpu_hosts": sorted(
                rows, key=lambda r: r.get("cpu_used_percent") or 0, reverse=True
            )[:5],
            "top_memory_hosts": sorted(
                rows, key=lambda r: r.get("memory_used_percent") or 0, reverse=True
            )[:5],
            "unhealthy_hosts": unhealthy,
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        await cache_set(key, result)
        return result

    async def _view_vm_health(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        cached, key = await self._cached(
            agent_context, client, params, "vm_health"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        vms = await self._vms(client)
        non_templates = [v for v in vms if not v.get("is_template")]
        powered_on = [v for v in non_templates if v.get("power_state") == "poweredOn"]

        power_dist: Counter = Counter(
            v.get("power_state") or "unknown" for v in non_templates
        )
        tools_dist: Counter = Counter(
            v.get("tools_status") or "unknown" for v in non_templates
        )
        tools_not_running = [
            {
                "name": v.get("name"), "host": v.get("host"),
                "power_state": v.get("power_state"),
                "tools_running": v.get("tools_running"),
            }
            for v in powered_on
            if v.get("tools_running") and v.get("tools_running") != "guestToolsRunning"
        ]

        result = {
            "vm_total": len(non_templates),
            "powered_on": len(powered_on),
            "templates": sum(1 for v in vms if v.get("is_template")),
            "power_state_distribution": dict(power_dist),
            "tools_status_distribution": dict(tools_dist),
            "top_cpu_consumers": sorted(
                ({
                    "name": v.get("name"), "host": v.get("host"),
                    "cpu_usage_mhz": v.get("cpu_usage_mhz"),
                } for v in powered_on),
                key=lambda r: r.get("cpu_usage_mhz") or 0, reverse=True,
            )[:10],
            "top_memory_consumers": sorted(
                ({
                    "name": v.get("name"), "host": v.get("host"),
                    "memory_usage_mb": v.get("memory_usage_mb"),
                } for v in powered_on),
                key=lambda r: r.get("memory_usage_mb") or 0, reverse=True,
            )[:10],
            "tools_not_running": tools_not_running[:25],
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        await cache_set(key, result)
        return result

    async def _view_datastore_summary(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        cached, key = await self._cached(
            agent_context, client, params, "datastore_summary"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        datastores = await self._datastores(client)
        rows: list[dict] = []
        for d in datastores:
            cap = d.get("capacity_bytes") or 0
            free = d.get("free_bytes") or 0
            used = cap - free
            rows.append({
                "name": d.get("name"),
                "type": d.get("type"),
                "accessible": d.get("accessible"),
                "capacity_gb": _gb_bytes(cap),
                "used_gb": _gb_bytes(used),
                "free_gb": _gb_bytes(free),
                "used_percent": round(used / cap * 100, 1) if cap else None,
                "num_vms": d.get("num_vms"),
            })
        near_full = [
            r for r in rows if (r.get("used_percent") or 0) >= _NEAR_FULL_PCT
        ]
        result = {
            "datastore_count": len(rows),
            "total_capacity_gb": round(_sum(r.get("capacity_gb") for r in rows), 2),
            "total_used_gb": round(_sum(r.get("used_gb") for r in rows), 2),
            "fullest_datastores": sorted(
                rows, key=lambda r: r.get("used_percent") or 0, reverse=True
            )[:10],
            "near_full_alert": near_full,
            "near_full_threshold_percent": _NEAR_FULL_PCT,
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        await cache_set(key, result)
        return result

    async def _view_capacity_report(
        self, client: VCenterClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        cached, key = await self._cached(
            agent_context, client, params, "capacity_report"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        hosts = await self._hosts(client)
        vms = await self._vms(client)
        connected = [h for h in hosts if h.get("connection_state") == "connected"]
        non_templates = [v for v in vms if not v.get("is_template")]

        physical_cores = _sum(h.get("cpu_cores") for h in connected)
        physical_mem_gb = _sum(_gb_bytes(h.get("memory_bytes")) for h in connected)
        allocated_vcpu = _sum(v.get("num_cpu") for v in non_templates)
        allocated_mem_gb = round(_sum(v.get("memory_mb") for v in non_templates) / 1024, 2)

        running = [v for v in non_templates if v.get("power_state") == "poweredOn"]
        running_vcpu = _sum(v.get("num_cpu") for v in running)
        running_mem_gb = round(_sum(v.get("memory_mb") for v in running) / 1024, 2)

        powered_off = [v for v in non_templates if v.get("power_state") != "poweredOn"]
        reclaimable_gb = round(
            _sum(_gb_bytes(v.get("storage_committed_bytes")) for v in powered_off), 2
        )

        result = {
            "physical": {
                "cores": physical_cores,
                "memory_gb": round(physical_mem_gb, 2),
            },
            "allocated_all_vms": {
                "vcpu": allocated_vcpu,
                "memory_gb": allocated_mem_gb,
                "vcpu_overcommit_ratio": (
                    round(allocated_vcpu / physical_cores, 2)
                    if physical_cores else None
                ),
                "memory_overcommit_ratio": (
                    round(allocated_mem_gb / physical_mem_gb, 2)
                    if physical_mem_gb else None
                ),
            },
            "allocated_powered_on": {
                "vcpu": running_vcpu,
                "memory_gb": running_mem_gb,
            },
            "waste": {
                "powered_off_vms": len(powered_off),
                "templates": sum(1 for v in vms if v.get("is_template")),
                "reclaimable_storage_gb_estimate": reclaimable_gb,
            },
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        await cache_set(key, result)
        return result


_ = Any
