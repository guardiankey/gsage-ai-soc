"""gSage AI — VMware vCenter read-only inventory tool.

Exposes an ``action`` enum dispatcher covering the read-only vSphere
queries SOC / ops analysts need when triaging or planning a change:

- ``list_datacenters``, ``list_clusters``, ``get_cluster``
- ``list_hosts``, ``get_host``
- ``list_vms`` (filters: cluster / host / folder / power_state /
  templates_only), ``get_vm`` (full config: vCPU, RAM, disks, NICs,
  guest/tools, datastores, networks, annotations)
- ``list_templates``, ``list_datastores``, ``list_networks``,
  ``list_resource_pools``, ``list_folders``
- ``find_vm`` (by ``ip`` / ``name`` / ``uuid``) — answer "which VM is
  this IP?" from a SOC alert
- ``list_snapshots`` (snapshot tree of one VM)
- ``recent_tasks`` / ``recent_events`` — vCenter audit trail
- ``get_vm_metrics`` (CPU / memory / disk / network real-time perf)

Tabular results are run through the shared ``result_export`` pipeline:
when over 100 rows, a CSV artifact is auto-generated and only the first
100 rows are inlined for the agent. ``export_csv=true`` forces CSV for
any size.

Permission: ``vcenter:read``. Multiple vCenters via per-profile config
(``supports_multiple_configs=True``).
"""

from __future__ import annotations

import logging
import time
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
from src.mcp_server.tools.result_export import (
    AGENT_PREVIEW_ROWS,
    build_agent_payload,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = frozenset({
    "list_datacenters",
    "list_clusters",
    "get_cluster",
    "list_hosts",
    "get_host",
    "list_vms",
    "get_vm",
    "list_templates",
    "list_datastores",
    "list_networks",
    "list_resource_pools",
    "list_folders",
    "find_vm",
    "list_snapshots",
    "recent_tasks",
    "recent_events",
    "get_vm_metrics",
})

_DEFAULT_RESULTS = 100
_MAX_RESULTS = 2000


class _ParamError(Exception):
    pass


def _require(params: dict, field: str) -> str:
    val = params.get(field)
    if isinstance(val, str):
        val = val.strip()
    if not val:
        raise _ParamError(f"'{field}' is required for this action.")
    return str(val)


class VCenterInventoryTool(BaseTool):
    """Read-only VMware vCenter inventory.

    Use one ``action`` per call. Tabular results auto-export as CSV when
    over 100 rows; the agent receives only the first 100 rows plus a
    download link.

    Permission: ``vcenter:read``.
    """

    name: ClassVar[str] = "vcenter_inventory"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Read-only VMware vCenter inventory: datacenters, clusters, hosts, "
        "VMs + config, templates, datastores, networks, snapshots, tasks/"
        "events, find-VM-by-IP, perf metrics. Auto-CSV on >100 rows."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "vcenter"
    permissions: ClassVar[list[str]] = ["vcenter:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 120
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "target_entities": "name",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which read-only inventory query to run.",
            },
            "profile": {
                "type": "string",
                "description": (
                    "Name of the configured vCenter to query (a key under "
                    "the config 'profiles' map). Omit (or 'default') for the "
                    "primary vCenter."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Target object name. Required by get_cluster, get_host, "
                    "get_vm, list_snapshots, get_vm_metrics. For find_vm, "
                    "matches the VM inventory/DNS name."
                ),
            },
            "ip": {
                "type": "string",
                "description": "[find_vm] Guest IP address to resolve to a VM.",
            },
            "uuid": {
                "type": "string",
                "description": "[find_vm] VM instance UUID (or BIOS UUID).",
            },
            "cluster": {
                "type": "string",
                "description": "[list_vms, list_hosts] Filter by cluster name.",
            },
            "host": {
                "type": "string",
                "description": "[list_vms] Filter by ESXi host name.",
            },
            "folder": {
                "type": "string",
                "description": "[list_vms] Filter by VM folder name.",
            },
            "power_state": {
                "type": "string",
                "enum": ["poweredOn", "poweredOff", "suspended"],
                "description": "[list_vms] Filter VMs by power state.",
            },
            "templates_only": {
                "type": "boolean",
                "description": (
                    "[list_vms] Return only templates (default false). "
                    "list_vms excludes templates by default."
                ),
            },
            "entity": {
                "type": "string",
                "description": (
                    "[recent_tasks, recent_events] Restrict to a single VM/"
                    "host/cluster name. Omit for vCenter-wide recent activity."
                ),
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_RESULTS,
                "description": (
                    f"Maximum items to return (default {_DEFAULT_RESULTS}, "
                    f"hard cap {_MAX_RESULTS})."
                ),
            },
            "force_refresh": {
                "type": "boolean",
                "description": (
                    "Bypass the Redis cache for this call (still writes the "
                    f"fresh value back). Cache TTL is {CACHE_TTL_SECONDS}s."
                ),
            },
            "export_csv": {
                "type": "boolean",
                "description": (
                    "Force CSV artifact even for small results. CSV is "
                    f"generated automatically over {AGENT_PREVIEW_ROWS} rows."
                ),
            },
            "export_json": {
                "type": "boolean",
                "description": (
                    "Persist the full result as a JSON artifact for "
                    "programmatic post-processing."
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
            async with build_vcenter_client(
                config, profile=params.get("profile")
            ) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(
                    client, params, agent_context, max_results
                )
        except _ParamError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INVALID_PARAMS", str(exc), execution_time_ms=elapsed
            )
        except VCenterError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            retryable = exc.code in ("CONNECTION_ERROR", "TIMEOUT")
            return self._failure(
                exc.code, str(exc), retryable=retryable,
                execution_time_ms=elapsed,
            )
        except Exception as exc:
            log.exception("vcenter_inventory(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={"action": action, **data}, execution_time_ms=elapsed
        )

    # ── Tabular helper (with cache) ─────────────────────────────────────────

    async def _tabular(
        self,
        agent_context: AgentContext,
        action: str,
        rows: list[dict],
        params: dict,
        *,
        cache_hit: bool = False,
    ) -> dict:
        agent_payload = await build_agent_payload(
            tool=self,
            rows=rows,
            export_csv=bool(params.get("export_csv", False)),
            export_json=bool(params.get("export_json", False)),
            filename_prefix=f"{self.name}_{action}",
            agent_context=agent_context,
        )
        return {
            "rows_total": agent_payload["rows_total"],
            "rows_overflow": agent_payload["rows_overflow"],
            "rows_preview_limit": AGENT_PREVIEW_ROWS,
            "artifacts": agent_payload["artifacts"],
            "agent_hint": agent_payload["agent_hint"],
            "rows": agent_payload["rows_preview"],
            "cache_hit": cache_hit,
        }

    async def _list(
        self,
        agent_context: AgentContext,
        client: VCenterClient,
        action: str,
        kind: str,
        params: dict,
        filters: dict,
        rows_fn: Any,
        max_results: int,
    ) -> dict:
        """Cache-aware list helper: look up cached rows, else build & cache."""
        org = str(getattr(agent_context, "org_id", "") or "")
        user = str(getattr(agent_context, "user_id", "") or "")
        profile = str(params.get("profile") or "default")
        key = build_cache_key(
            org_id=org, user_id=user, profile_id=profile,
            vcenter_host=client.host, kind=kind, filters=filters,
        )
        if not params.get("force_refresh"):
            cached = await cache_get(key)
            if isinstance(cached, list):
                return await self._tabular(
                    agent_context, action, cached[:max_results], params,
                    cache_hit=True,
                )
        rows = await rows_fn()
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, action, rows[:max_results], params
        )

    # ── List actions ────────────────────────────────────────────────────────

    async def _do_list_datacenters(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        async def _rows() -> list[dict]:
            objs = await client.list_objs("Datacenter")
            return [V.slim_datacenter(o) for o in objs]
        return await self._list(
            agent_context, client, "list_datacenters", "datacenters",
            params, {}, _rows, max_results,
        )

    async def _do_list_clusters(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        async def _rows() -> list[dict]:
            objs = await client.list_objs("ClusterComputeResource")
            return [V.slim_cluster(o) for o in objs]
        return await self._list(
            agent_context, client, "list_clusters", "clusters",
            params, {}, _rows, max_results,
        )

    async def _do_list_hosts(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        cluster = (params.get("cluster") or "").strip()

        async def _rows() -> list[dict]:
            if cluster:
                cl = await client.get_obj("ClusterComputeResource", cluster)
                hosts = await client.call(lambda: list(cl.host))
            else:
                hosts = await client.list_objs("HostSystem")
            return [V.slim_host(h) for h in hosts]
        return await self._list(
            agent_context, client, "list_hosts", "hosts",
            params, {"cluster": cluster}, _rows, max_results,
        )

    async def _do_list_vms(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        cluster = (params.get("cluster") or "").strip()
        host = (params.get("host") or "").strip()
        folder = (params.get("folder") or "").strip()
        power_state = (params.get("power_state") or "").strip()
        templates_only = bool(params.get("templates_only"))
        filters = {
            "cluster": cluster, "host": host, "folder": folder,
            "power_state": power_state, "templates_only": templates_only,
        }

        async def _rows() -> list[dict]:
            vms = await client.list_objs("VirtualMachine")
            rows = [V.slim_vm(vm) for vm in vms]
            if templates_only:
                rows = [r for r in rows if r.get("is_template")]
            else:
                rows = [r for r in rows if not r.get("is_template")]
            if host:
                rows = [r for r in rows if r.get("host") == host]
            if folder:
                rows = [r for r in rows if r.get("folder") == folder]
            if power_state:
                rows = [r for r in rows if r.get("power_state") == power_state]
            if cluster:
                # Resolve the cluster's host set once, then filter by host.
                cl = await client.get_obj("ClusterComputeResource", cluster)
                cluster_hosts = set(
                    await client.call(lambda: [h.name for h in cl.host])
                )
                rows = [r for r in rows if r.get("host") in cluster_hosts]
            return rows
        return await self._list(
            agent_context, client, "list_vms", "vms",
            params, filters, _rows, max_results,
        )

    async def _do_list_templates(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        async def _rows() -> list[dict]:
            vms = await client.list_objs("VirtualMachine")
            rows = [V.slim_vm(vm) for vm in vms]
            return [r for r in rows if r.get("is_template")]
        return await self._list(
            agent_context, client, "list_templates", "templates",
            params, {}, _rows, max_results,
        )

    async def _do_list_datastores(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        async def _rows() -> list[dict]:
            objs = await client.list_objs("Datastore")
            return [V.slim_datastore(o) for o in objs]
        return await self._list(
            agent_context, client, "list_datastores", "datastores",
            params, {}, _rows, max_results,
        )

    async def _do_list_networks(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        async def _rows() -> list[dict]:
            objs = await client.list_objs("Network")
            return [V.slim_network(o) for o in objs]
        return await self._list(
            agent_context, client, "list_networks", "networks",
            params, {}, _rows, max_results,
        )

    async def _do_list_resource_pools(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        async def _rows() -> list[dict]:
            objs = await client.list_objs("ResourcePool")
            return [V.slim_resource_pool(o) for o in objs]
        return await self._list(
            agent_context, client, "list_resource_pools", "resource_pools",
            params, {}, _rows, max_results,
        )

    async def _do_list_folders(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        async def _rows() -> list[dict]:
            objs = await client.list_objs("Folder")
            return [V.slim_folder(o) for o in objs]
        return await self._list(
            agent_context, client, "list_folders", "folders",
            params, {}, _rows, max_results,
        )

    # ── Single-object actions ────────────────────────────────────────────────

    async def _do_get_cluster(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        cl = await client.get_obj("ClusterComputeResource", _require(params, "name"))
        return {"cluster": await client.call(lambda: V.slim_cluster(cl, detail=True))}

    async def _do_get_host(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        h = await client.get_obj("HostSystem", _require(params, "name"))
        return {"host": await client.call(lambda: V.slim_host(h, detail=True))}

    async def _do_get_vm(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        vm = await client.find_vm(name=_require(params, "name"))
        return {"vm": await client.call(lambda: V.slim_vm(vm, detail=True))}

    async def _do_find_vm(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        vm = await client.find_vm(
            name=(params.get("name") or "").strip() or None,
            ip=(params.get("ip") or "").strip() or None,
            uuid=(params.get("uuid") or "").strip() or None,
        )
        return {"vm": await client.call(lambda: V.slim_vm(vm, detail=True))}

    async def _do_list_snapshots(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        vm = await client.find_vm(name=_require(params, "name"))
        snaps = await client.call(lambda: V.slim_snapshot_tree(vm))
        return {"vm_name": params.get("name"), "snapshot_count": len(snaps), "snapshots": snaps}

    async def _do_recent_tasks(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        entity = (params.get("entity") or "").strip()

        def _collect() -> list[dict]:
            content = client._content()  # noqa: SLF001 — intra-package access
            tm = content.taskManager
            infos = list(tm.recentTask or [])
            rows = [V.slim_task(t.info) for t in infos]
            if entity:
                rows = [r for r in rows if r.get("entity") == entity]
            return rows
        rows = await client.call(_collect)
        return await self._tabular(
            agent_context, "recent_tasks", rows[:max_results], params
        )

    async def _do_recent_events(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        entity = (params.get("entity") or "").strip()

        def _collect() -> list[dict]:
            content = client._content()  # noqa: SLF001 — intra-package access
            em = content.eventManager
            events = list(em.latestEvent and [em.latestEvent] or [])
            # latestEvent is a single event; for a window we read the
            # collector's latest page (bounded) when available.
            try:
                spec = client.vim.event.EventFilterSpec()
                collector = em.CreateCollectorForEvents(spec)
                collector.SetCollectorPageSize(min(max_results, 1000))
                page = list(collector.latestPage or [])
                collector.DestroyCollector()
                events = page or events
            except Exception:
                log.debug("vcenter: event collector unavailable", exc_info=True)
            rows = [V.slim_event(e) for e in events]
            if entity:
                rows = [
                    r for r in rows
                    if entity in (r.get("vm"), r.get("host"), r.get("datacenter"))
                ]
            return rows
        rows = await client.call(_collect)
        return await self._tabular(
            agent_context, "recent_events", rows[:max_results], params
        )

    async def _do_get_vm_metrics(
        self, client: VCenterClient, params: dict,
        agent_context: AgentContext, max_results: int,
    ) -> dict:
        vm = await client.find_vm(name=_require(params, "name"))

        def _metrics() -> dict:
            summ = vm.summary
            qs = summ.quickStats
            cfg = summ.config
            return {
                "cpu_usage_mhz": getattr(qs, "overallCpuUsage", None),
                "cpu_demand_mhz": getattr(qs, "overallCpuDemand", None),
                "host_memory_usage_mb": getattr(qs, "hostMemoryUsage", None),
                "guest_memory_usage_mb": getattr(qs, "guestMemoryUsage", None),
                "ballooned_memory_mb": getattr(qs, "balloonedMemory", None),
                "swapped_memory_mb": getattr(qs, "swappedMemory", None),
                "uptime_seconds": getattr(qs, "uptimeSeconds", None),
                "num_cpu": getattr(cfg, "numCpu", None),
                "memory_mb": getattr(cfg, "memorySizeMB", None),
                "storage_committed_bytes": getattr(summ.storage, "committed", None),
                "storage_uncommitted_bytes": getattr(summ.storage, "uncommitted", None),
            }
        metrics = await client.call(_metrics)
        return {"vm_name": params.get("name"), "metrics": metrics}


_ = Any
