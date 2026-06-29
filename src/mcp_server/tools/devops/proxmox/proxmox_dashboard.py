"""gSage AI — High-level Proxmox VE dashboards (read-only summaries).

Composes the lower-level inventory primitives (``/nodes``,
``/cluster/resources``, ``/cluster/status``) into opinionated,
ops-friendly summaries. Each view returns a JSON object designed to be
rendered as a dashboard tile.

Available views:

- ``cluster_overview``   — Node up/down, quorum, guest counts (QEMU/LXC,
                          running/stopped, templates), aggregate capacity
                          vs usage.
- ``node_health``         — Per-node CPU / memory / disk usage, guest
                          counts, and the busiest / unhealthy nodes.
- ``guest_health``        — Power-status distribution by kind, top CPU and
                          memory consumers, stopped guests still holding
                          disk.
- ``storage_summary``     — Storage capacity/usage across the cluster, the
                          fullest stores, and a >85 %% near-full alert.
- ``capacity_report``     — Memory / vCPU overcommit ratios (allocated vs
                          physical) and reclaimable waste (stopped guests,
                          templates).

Permission: ``proxmox:read``. Cache TTL 300s.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.devops.proxmox._cache import (
    CACHE_TTL_SECONDS,
    build_cache_key,
    cache_get,
    cache_set,
)
from src.mcp_server.tools.devops.proxmox._client import (
    PROXMOX_CONFIG_DEFAULTS,
    PROXMOX_CONFIG_SCHEMA,
    ProxmoxClient,
    ProxmoxError,
    build_proxmox_client,
)
from src.mcp_server.tools.devops.proxmox import _views as V
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_VIEWS = frozenset({
    "cluster_overview",
    "node_health",
    "guest_health",
    "storage_summary",
    "capacity_report",
})

_NEAR_FULL_PCT = 85.0


def _sum(values: Any) -> float:
    return float(sum(v for v in values if isinstance(v, (int, float))))


class ProxmoxDashboardTool(BaseTool):
    """Aggregated read-only Proxmox VE dashboards.

    Use ``view`` to select a tile. Heavy queries are cached for 5 minutes
    per (org, user, profile, host, view).

    Permission: ``proxmox:read``.
    """

    name: ClassVar[str] = "proxmox_dashboard"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Proxmox VE dashboards: cluster overview, node health, guest "
        "health, storage summary, capacity/overcommit report."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "proxmox"
    permissions: ClassVar[list[str]] = ["proxmox:read"]
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
                    "Name of the configured Proxmox cluster to summarize (a "
                    "key under the config 'profiles' map). Omit (or 'default') "
                    "for the primary cluster."
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

    config_schema: ClassVar[Optional[dict]] = PROXMOX_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = PROXMOX_CONFIG_DEFAULTS
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
            async with build_proxmox_client(
                config, profile=params.get("profile")
            ) as client:
                handler = getattr(self, f"_view_{view}")
                data = await handler(client, params, agent_context)
        except ProxmoxError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.code in ("CONNECTION_ERROR", "TIMEOUT", "RATE_LIMITED")
            return self._failure(
                exc.code, str(exc), retryable=retryable,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("proxmox_dashboard(%s): unexpected error", view)
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
        client: ProxmoxClient,
        params: dict,
        view: str,
    ) -> tuple[Optional[dict], str]:
        org = str(getattr(agent_context, "org_id", "") or "")
        user = str(getattr(agent_context, "user_id", "") or "")
        profile = str(params.get("profile") or "default")
        key = build_cache_key(
            org_id=org, user_id=user, profile_id=profile,
            pve_host=client.host, kind=f"dashboard:{view}", filters={},
        )
        if params.get("force_refresh"):
            return None, key
        cached = await cache_get(key)
        if isinstance(cached, dict):
            return cached, key
        return None, key

    # ── Shared fetch helpers ─────────────────────────────────────────────────

    async def _nodes(self, client: ProxmoxClient) -> list[dict]:
        return [V.slim_node(n) for n in (await client.get("/nodes") or [])]

    async def _guests(self, client: ProxmoxClient) -> list[dict]:
        return [
            V.slim_guest_resource(r)
            for r in await client.cluster_resources("vm")
        ]

    # ── Views ──────────────────────────────────────────────────────────────

    async def _view_cluster_overview(
        self, client: ProxmoxClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        cached, key = await self._cached(
            agent_context, client, params, "cluster_overview"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        nodes = await self._nodes(client)
        guests = await self._guests(client)
        online = [n for n in nodes if n.get("status") == "online"]

        # Quorum (standalone hosts report no cluster row).
        quorate: Optional[bool] = None
        cluster_name: Optional[str] = None
        try:
            for row in await client.get("/cluster/status") or []:
                if row.get("type") == "cluster":
                    quorate = bool(row.get("quorate"))
                    cluster_name = row.get("name")
        except ProxmoxError as exc:
            log.debug("cluster status failed: %s", exc)

        non_templates = [g for g in guests if not g.get("is_template")]
        counts = {
            "nodes_total": len(nodes),
            "nodes_online": len(online),
            "nodes_offline": len(nodes) - len(online),
            "qemu_running": sum(
                1 for g in non_templates
                if g.get("kind") == "qemu" and g.get("status") == "running"
            ),
            "qemu_stopped": sum(
                1 for g in non_templates
                if g.get("kind") == "qemu" and g.get("status") != "running"
            ),
            "lxc_running": sum(
                1 for g in non_templates
                if g.get("kind") == "lxc" and g.get("status") == "running"
            ),
            "lxc_stopped": sum(
                1 for g in non_templates
                if g.get("kind") == "lxc" and g.get("status") != "running"
            ),
            "templates": sum(1 for g in guests if g.get("is_template")),
        }
        capacity = {
            "physical_cores": _sum(n.get("max_cpu") for n in online),
            "memory_total_gb": round(_sum(n.get("max_memory_gb") for n in online), 2),
            "memory_used_gb": round(_sum(n.get("memory_gb") for n in online), 2),
            "disk_total_gb": round(_sum(n.get("max_disk_gb") for n in online), 2),
            "disk_used_gb": round(_sum(n.get("disk_gb") for n in online), 2),
        }
        if capacity["memory_total_gb"]:
            capacity["memory_used_percent"] = round(
                capacity["memory_used_gb"] / capacity["memory_total_gb"] * 100, 1
            )

        result = {
            "cluster_name": cluster_name,
            "quorate": quorate,
            "counts": counts,
            "capacity": capacity,
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        await cache_set(key, result)
        return result

    async def _view_node_health(
        self, client: ProxmoxClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        cached, key = await self._cached(
            agent_context, client, params, "node_health"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        nodes = await self._nodes(client)
        guests = await self._guests(client)
        guests_by_node: Counter = Counter(
            g.get("node") for g in guests if not g.get("is_template")
        )

        rows: list[dict] = []
        for n in nodes:
            mem_total = n.get("max_memory_gb") or 0
            mem_used = n.get("memory_gb") or 0
            mem_pct = round(mem_used / mem_total * 100, 1) if mem_total else None
            rows.append({
                "node": n.get("node"),
                "status": n.get("status"),
                "cpu_percent": n.get("cpu_percent"),
                "memory_used_gb": mem_used,
                "memory_total_gb": mem_total,
                "memory_used_percent": mem_pct,
                "disk_used_gb": n.get("disk_gb"),
                "disk_total_gb": n.get("max_disk_gb"),
                "uptime_seconds": n.get("uptime_seconds"),
                "guest_count": guests_by_node.get(n.get("node"), 0),
            })

        unhealthy = [
            r for r in rows
            if r.get("status") != "online"
            or (r.get("memory_used_percent") or 0) >= _NEAR_FULL_PCT
        ]
        result = {
            "node_count": len(rows),
            "nodes": rows,
            "top_cpu_nodes": sorted(
                rows, key=lambda r: r.get("cpu_percent") or 0, reverse=True
            )[:5],
            "top_memory_nodes": sorted(
                rows, key=lambda r: r.get("memory_used_percent") or 0,
                reverse=True,
            )[:5],
            "unhealthy_nodes": unhealthy,
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        await cache_set(key, result)
        return result

    async def _view_guest_health(
        self, client: ProxmoxClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        cached, key = await self._cached(
            agent_context, client, params, "guest_health"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        guests = await self._guests(client)
        non_templates = [g for g in guests if not g.get("is_template")]
        running = [g for g in non_templates if g.get("status") == "running"]
        stopped = [g for g in non_templates if g.get("status") != "running"]

        status_by_kind: dict[str, Counter] = {"qemu": Counter(), "lxc": Counter()}
        for g in non_templates:
            kind = g.get("kind") or "qemu"
            status_by_kind.setdefault(kind, Counter())[g.get("status") or "unknown"] += 1

        # Stopped guests still occupying disk are reclaim candidates.
        stopped_with_disk = sorted(
            ({
                "vmid": g.get("vmid"), "name": g.get("name"),
                "kind": g.get("kind"), "node": g.get("node"),
                "max_disk_gb": g.get("max_disk_gb"),
            } for g in stopped if (g.get("max_disk_gb") or 0) > 0),
            key=lambda r: r.get("max_disk_gb") or 0, reverse=True,
        )

        result = {
            "guest_total": len(non_templates),
            "running": len(running),
            "stopped": len(stopped),
            "templates": sum(1 for g in guests if g.get("is_template")),
            "status_by_kind": {k: dict(c) for k, c in status_by_kind.items()},
            "top_cpu_consumers": sorted(
                ({
                    "vmid": g.get("vmid"), "name": g.get("name"),
                    "kind": g.get("kind"), "node": g.get("node"),
                    "cpu_percent": g.get("cpu_percent"),
                } for g in running),
                key=lambda r: r.get("cpu_percent") or 0, reverse=True,
            )[:10],
            "top_memory_consumers": sorted(
                ({
                    "vmid": g.get("vmid"), "name": g.get("name"),
                    "kind": g.get("kind"), "node": g.get("node"),
                    "memory_gb": g.get("memory_gb"),
                } for g in running),
                key=lambda r: r.get("memory_gb") or 0, reverse=True,
            )[:10],
            "stopped_with_disk": stopped_with_disk[:25],
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        await cache_set(key, result)
        return result

    async def _view_storage_summary(
        self, client: ProxmoxClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        cached, key = await self._cached(
            agent_context, client, params, "storage_summary"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        # /cluster/resources?type=storage exposes one row per (storage, node);
        # dedup shared stores by name (their totals are identical cluster-wide).
        raw = await client.cluster_resources("storage")
        by_name: dict[str, dict] = {}
        for r in raw:
            name = r.get("storage")
            if not name:
                continue
            total = float(r.get("maxdisk") or 0)
            used = float(r.get("disk") or 0)
            shared = str(r.get("shared")) in ("1", "True", "true")
            existing = by_name.get(name)
            if existing is None or (shared and total >= existing["_total_bytes"]):
                by_name[name] = {
                    "storage": name,
                    "type": r.get("plugintype") or r.get("type"),
                    "shared": shared,
                    "node": None if shared else r.get("node"),
                    "content": r.get("content"),
                    "total_gb": round(total / (1024 ** 3), 2),
                    "used_gb": round(used / (1024 ** 3), 2),
                    "used_percent": round(used / total * 100, 1) if total else None,
                    "_total_bytes": total,
                }
        stores = list(by_name.values())
        for s in stores:
            s.pop("_total_bytes", None)

        near_full = [
            s for s in stores
            if (s.get("used_percent") or 0) >= _NEAR_FULL_PCT
        ]
        result = {
            "storage_count": len(stores),
            "total_capacity_gb": round(_sum(s.get("total_gb") for s in stores), 2),
            "total_used_gb": round(_sum(s.get("used_gb") for s in stores), 2),
            "fullest_storages": sorted(
                stores, key=lambda s: s.get("used_percent") or 0, reverse=True
            )[:10],
            "near_full_alert": near_full,
            "near_full_threshold_percent": _NEAR_FULL_PCT,
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        await cache_set(key, result)
        return result

    async def _view_capacity_report(
        self, client: ProxmoxClient, params: dict, agent_context: AgentContext,
    ) -> dict:
        cached, key = await self._cached(
            agent_context, client, params, "capacity_report"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        nodes = await self._nodes(client)
        guests = await self._guests(client)
        online = [n for n in nodes if n.get("status") == "online"]
        non_templates = [g for g in guests if not g.get("is_template")]

        physical_cores = _sum(n.get("max_cpu") for n in online)
        physical_mem_gb = _sum(n.get("max_memory_gb") for n in online)
        # Allocated = sum across all non-template guests (running or not).
        allocated_vcpu = _sum(g.get("max_cpu") for g in non_templates)
        allocated_mem_gb = _sum(g.get("max_memory_gb") for g in non_templates)
        # Running-only allocation (true pressure right now).
        running = [g for g in non_templates if g.get("status") == "running"]
        running_vcpu = _sum(g.get("max_cpu") for g in running)
        running_mem_gb = _sum(g.get("max_memory_gb") for g in running)

        stopped = [g for g in non_templates if g.get("status") != "running"]
        reclaimable_disk_gb = round(_sum(g.get("max_disk_gb") for g in stopped), 2)

        result = {
            "physical": {
                "cores": physical_cores,
                "memory_gb": round(physical_mem_gb, 2),
            },
            "allocated_all_guests": {
                "vcpu": allocated_vcpu,
                "memory_gb": round(allocated_mem_gb, 2),
                "vcpu_overcommit_ratio": (
                    round(allocated_vcpu / physical_cores, 2)
                    if physical_cores else None
                ),
                "memory_overcommit_ratio": (
                    round(allocated_mem_gb / physical_mem_gb, 2)
                    if physical_mem_gb else None
                ),
            },
            "allocated_running_only": {
                "vcpu": running_vcpu,
                "memory_gb": round(running_mem_gb, 2),
                "memory_commit_percent": (
                    round(running_mem_gb / physical_mem_gb * 100, 1)
                    if physical_mem_gb else None
                ),
            },
            "waste": {
                "stopped_guests": len(stopped),
                "templates": sum(1 for g in guests if g.get("is_template")),
                "reclaimable_disk_gb_estimate": reclaimable_disk_gb,
            },
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        await cache_set(key, result)
        return result


_ = Any
