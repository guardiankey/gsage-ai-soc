"""gSage AI — High-level Azure dashboards (read-only summaries).

Composes the lower-level inventory / metrics / costs primitives into
opinionated, SOC-friendly summaries. Each view returns a JSON object
designed to be rendered as a dashboard tile.

Available views:

- ``subscription_overview``   — Subs + RG + resource counts + MTD cost.
- ``compute_health``           — VM power-state distribution, top CPU
                                consumers, idle/oversized counts.
- ``cost_summary``             — MTD total + top services + top RGs.
- ``waste_report``             — Orphan resources + idle VMs +
                                consolidated potential savings.
- ``aks_overview``             — AKS clusters with versions, node counts
                                and provisioning state.
- ``database_overview``        — SQL servers + DB SKU/status breakdown.

Permission: ``azure:read``. Cache TTL 300s.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
from src.mcp_server.tools.devops.azure._cache import (
    CACHE_TTL_SECONDS,
    build_cache_key,
    cache_get,
    cache_set,
)
from src.mcp_server.tools.devops.azure._client import (
    AZURE_CONFIG_DEFAULTS,
    AZURE_CONFIG_SCHEMA,
    AzureClient,
    AzureError,
    build_azure_client,
)
from src.mcp_server.tools.devops.azure._recommendations import (
    consolidate_savings,
    detect_idle_vm,
    detect_old_snapshot,
    detect_orphan_disk,
    detect_orphan_nic,
    detect_orphan_public_ip,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_VIEWS = frozenset({
    "subscription_overview",
    "compute_health",
    "cost_summary",
    "waste_report",
    "aks_overview",
    "database_overview",
})


class AzureDashboardTool(BaseTool):
    """Aggregated read-only Azure dashboards.

    Use ``view`` to select a tile. Heavy queries are cached for 5
    minutes per (org, user, profile, subscription, view).

    Permission: ``azure:read``.
    """

    name: ClassVar[str] = "azure_dashboard"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Azure dashboards: subscription overview, compute health, cost "
        "summary, waste report, AKS, databases."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "azure"
    permissions: ClassVar[list[str]] = ["azure:read"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 180
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "view",
        "target_resource": "subscription_id",
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
            "profile": {"type": "string"},
            "subscription_id": {"type": "string"},
            "force_refresh": {
                "type": "boolean",
                "description": (
                    f"Bypass the Redis cache (TTL {CACHE_TTL_SECONDS}s)."
                ),
            },
        },
        "additionalProperties": False,
    }

    config_schema: ClassVar[Optional[dict]] = AZURE_CONFIG_SCHEMA
    config_defaults: ClassVar[dict] = AZURE_CONFIG_DEFAULTS
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
            async with build_azure_client(config) as client:
                handler = getattr(self, f"_view_{view}")
                data = await handler(client, params, agent_context)
        except AzureError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code, str(exc), execution_time_ms=elapsed
            )
        except Exception as exc:
            log.exception("azure_dashboard(%s): unexpected error", view)
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
        params: dict,
        sub_id: str,
        kind: str,
    ) -> tuple[Optional[dict], str]:
        org = str(getattr(agent_context, "org_id", "") or "")
        user = str(getattr(agent_context, "user_id", "") or "")
        profile = str(params.get("profile") or "default")
        key = build_cache_key(
            org_id=org,
            user_id=user,
            profile_id=profile,
            subscription_id=sub_id,
            kind=f"dashboard:{kind}",
            filters={},
        )
        if params.get("force_refresh"):
            return None, key
        cached = await cache_get(key)
        if isinstance(cached, dict):
            return cached, key
        return None, key

    # ── Views ──────────────────────────────────────────────────────────────

    async def _view_subscription_overview(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        cached, key = await self._cached(
            agent_context, params, sub_id, "subscription_overview"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        rc = client.resource(sub_id)
        rgs = await client.collect(rc.resource_groups.list(), limit=2000)
        cc = client.compute(sub_id)
        nc = client.network(sub_id)
        sc = client.storage(sub_id)
        wc = client.web(sub_id)

        vms = await client.collect(cc.virtual_machines.list_all(), limit=5000)
        disks = await client.collect(cc.disks.list(), limit=5000)
        pips = await client.collect(
            nc.public_ip_addresses.list_all(), limit=5000
        )
        nics = await client.collect(
            nc.network_interfaces.list_all(), limit=5000
        )
        storage_accs = await client.collect(
            sc.storage_accounts.list(), limit=5000
        )
        web_apps = await client.collect(wc.web_apps.list(), limit=5000)

        # MTD cost (best-effort; may fail on subs without entitlement).
        mtd_cost: Optional[float] = None
        currency: Optional[str] = None
        try:
            from src.mcp_server.tools.devops.azure.azure_costs import (
                _query_usage,
            )
            rows, currency = await _query_usage(
                client, sub_id, timeframe="MonthToDate",
                grouping_name="ServiceName",
                grouping_type="Dimension",
            )
            mtd_cost = round(sum(float(r.get("cost") or 0) for r in rows), 4)
        except AzureError as exc:
            log.debug("MTD cost query failed: %s", exc)

        result = {
            "subscription_id": sub_id,
            "resource_group_count": len(rgs),
            "counts": {
                "virtual_machines": len(vms),
                "disks": len(disks),
                "public_ips": len(pips),
                "network_interfaces": len(nics),
                "storage_accounts": len(storage_accs),
                "web_apps": len(web_apps),
            },
            "cost_mtd": {
                "total": mtd_cost,
                "currency": currency,
            },
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        await cache_set(key, result)
        return result

    async def _view_compute_health(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        cached, key = await self._cached(
            agent_context, params, sub_id, "compute_health"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        cc = client.compute(sub_id)
        mc = client.monitor(sub_id)
        vms = await client.collect(cc.virtual_machines.list_all(), limit=500)

        power_states: Counter = Counter()
        size_dist: Counter = Counter()
        location_dist: Counter = Counter()
        idle_count = 0
        top_cpu: list[dict] = []

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        timespan = f"{start.isoformat()}/{end.isoformat()}"
        interval = timedelta(hours=1)

        for vm in vms[:200]:  # cap heavy work
            d = _as_dict(vm)
            rid = d.get("id")
            name = d.get("name")
            vm_rg = _rg_from_id(rid)
            size = ((d.get("hardware_profile") or {}).get("vm_size")) or "?"
            size_dist[size] += 1
            if loc := d.get("location"):
                location_dist[loc] += 1

            ps: Optional[str] = None
            cpu_avg: Optional[float] = None
            net_avg: Optional[float] = None
            if vm_rg and name:
                try:
                    iv = await client.call(
                        cc.virtual_machines.instance_view(  # type: ignore[attr-defined]
                            vm_rg, name
                        )
                    )
                    ps = _power_state_from_iv(iv)
                except AzureError:
                    pass
            power_states[ps or "unknown"] += 1

            if rid and ps == "running":
                try:
                    res = await client.call(mc.metrics.list(
                        resource_uri=rid,
                        timespan=timespan,
                        interval=interval,
                        metricnames="Percentage CPU,Network In Total",
                        aggregation="Average",
                    ))
                    md = _as_dict(res)
                    cpu_avg = _avg_metric(md, "Percentage CPU")
                    net_avg = _avg_metric(md, "Network In Total")
                except AzureError:
                    pass
            if cpu_avg is not None:
                top_cpu.append({
                    "name": name,
                    "id": rid,
                    "cpu_avg_pct_7d": round(cpu_avg, 2),
                    "size": size,
                })
                if detect_idle_vm(
                    d, cpu_avg_pct=cpu_avg, net_bytes_avg=net_avg,
                ):
                    idle_count += 1

        top_cpu.sort(
            key=lambda r: r.get("cpu_avg_pct_7d") or 0, reverse=True
        )
        result = {
            "subscription_id": sub_id,
            "vm_total": len(vms),
            "vm_sampled": min(len(vms), 200),
            "power_states": dict(power_states),
            "size_distribution": dict(size_dist.most_common(10)),
            "location_distribution": dict(location_dist),
            "idle_vm_count_estimate": idle_count,
            "top_cpu_consumers": top_cpu[:10],
            "as_of": end.isoformat(),
        }
        await cache_set(key, result)
        return result

    async def _view_cost_summary(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        cached, key = await self._cached(
            agent_context, params, sub_id, "cost_summary"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        from src.mcp_server.tools.devops.azure.azure_costs import _query_usage

        result: dict[str, Any] = {
            "subscription_id": sub_id,
            "currency": None,
            "total_mtd": None,
            "top_services": [],
            "top_resource_groups": [],
        }
        try:
            services, currency = await _query_usage(
                client, sub_id, timeframe="MonthToDate",
                grouping_name="ServiceName",
                grouping_type="Dimension",
            )
            services.sort(key=lambda r: r.get("cost") or 0, reverse=True)
            result["currency"] = currency
            result["total_mtd"] = round(
                sum(float(r.get("cost") or 0) for r in services), 4
            )
            result["top_services"] = services[:10]
        except AzureError as exc:
            log.debug("cost summary services query failed: %s", exc)
        try:
            rgs, _currency2 = await _query_usage(
                client, sub_id, timeframe="MonthToDate",
                grouping_name="ResourceGroupName",
                grouping_type="Dimension",
            )
            rgs.sort(key=lambda r: r.get("cost") or 0, reverse=True)
            result["top_resource_groups"] = rgs[:10]
        except AzureError as exc:
            log.debug("cost summary RG query failed: %s", exc)

        result["as_of"] = datetime.now(timezone.utc).isoformat()
        await cache_set(key, result)
        return result

    async def _view_waste_report(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        cached, key = await self._cached(
            agent_context, params, sub_id, "waste_report"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        cc = client.compute(sub_id)
        nc = client.network(sub_id)

        recs: list[dict] = []

        # Orphan disks
        try:
            for d in await client.collect(cc.disks.list(), limit=5000):
                dd = _as_dict(d)
                if not dd.get("managed_by"):
                    rec = detect_orphan_disk(dd, cost_brl=None)
                    if rec:
                        recs.append(rec)
        except AzureError as exc:
            log.debug("orphan disks failed: %s", exc)

        # Orphan public IPs
        try:
            for p in await client.collect(
                nc.public_ip_addresses.list_all(), limit=5000
            ):
                pd = _as_dict(p)
                if not pd.get("ip_configuration"):
                    rec = detect_orphan_public_ip(pd, cost_brl=None)
                    if rec:
                        recs.append(rec)
        except AzureError as exc:
            log.debug("orphan IPs failed: %s", exc)

        # Orphan NICs
        try:
            for n in await client.collect(
                nc.network_interfaces.list_all(), limit=5000
            ):
                nd = _as_dict(n)
                if not nd.get("virtual_machine"):
                    rec = detect_orphan_nic(nd)
                    if rec:
                        recs.append(rec)
        except AzureError as exc:
            log.debug("orphan NICs failed: %s", exc)

        # Old snapshots
        try:
            for s in await client.collect(cc.snapshots.list(), limit=5000):
                sd = _as_dict(s)
                rec = detect_old_snapshot(sd, cost_brl=None)
                if rec:
                    recs.append(rec)
        except AzureError as exc:
            log.debug("old snapshots failed: %s", exc)

        savings = consolidate_savings(recs)
        # Group counts
        by_type: Counter = Counter()
        for r in recs:
            by_type[r.get("type", "unknown")] += 1
        result = {
            "subscription_id": sub_id,
            "total_findings": len(recs),
            "findings_by_type": dict(by_type),
            "savings": savings,
            "top_findings": recs[:25],
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        await cache_set(key, result)
        return result

    async def _view_aks_overview(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        cached, key = await self._cached(
            agent_context, params, sub_id, "aks_overview"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        ac = client.aks(sub_id)
        clusters = await client.collect(
            ac.managed_clusters.list(), limit=1000
        )
        rows: list[dict] = []
        version_dist: Counter = Counter()
        state_dist: Counter = Counter()
        node_total = 0
        for c in clusters:
            d = _as_dict(c)
            pools = d.get("agent_pool_profiles") or []
            count = sum(int(p.get("count") or 0) for p in pools)
            node_total += count
            ver = d.get("kubernetes_version") or "?"
            version_dist[ver] += 1
            state_dist[d.get("provisioning_state") or "?"] += 1
            rows.append({
                "name": d.get("name"),
                "id": d.get("id"),
                "resource_group": _rg_from_id(d.get("id")),
                "location": d.get("location"),
                "kubernetes_version": ver,
                "provisioning_state": d.get("provisioning_state"),
                "power_state": (d.get("power_state") or {}).get("code"),
                "node_count_total": count,
                "node_pool_count": len(pools),
                "sku_tier": (d.get("sku") or {}).get("tier"),
            })
        result = {
            "subscription_id": sub_id,
            "cluster_count": len(clusters),
            "node_total": node_total,
            "version_distribution": dict(version_dist),
            "provisioning_state_distribution": dict(state_dist),
            "clusters": rows,
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        await cache_set(key, result)
        return result

    async def _view_database_overview(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        cached, key = await self._cached(
            agent_context, params, sub_id, "database_overview"
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        sc = client.sql(sub_id)
        servers = await client.collect(sc.servers.list(), limit=1000)
        server_rows: list[dict] = []
        db_total = 0
        sku_dist: Counter = Counter()
        status_dist: Counter = Counter()
        for s in servers:
            sd = _as_dict(s)
            srg = _rg_from_id(sd.get("id"))
            sname = sd.get("name")
            db_count = 0
            if srg and sname:
                try:
                    dbs = await client.collect(
                        sc.databases.list_by_server(srg, sname), limit=2000
                    )
                except AzureError as exc:
                    log.debug("list dbs %s/%s failed: %s", srg, sname, exc)
                    dbs = []
                for db in dbs:
                    dd = _as_dict(db)
                    if (dd.get("name") or "").lower() == "master":
                        continue
                    db_count += 1
                    sku_dist[(dd.get("sku") or {}).get("name") or "?"] += 1
                    status_dist[dd.get("status") or "?"] += 1
            db_total += db_count
            server_rows.append({
                "name": sname,
                "id": sd.get("id"),
                "resource_group": srg,
                "location": sd.get("location"),
                "version": sd.get("version"),
                "fully_qualified_domain_name": sd.get(
                    "fully_qualified_domain_name"
                ),
                "database_count": db_count,
                "public_network_access": sd.get("public_network_access"),
            })
        result = {
            "subscription_id": sub_id,
            "server_count": len(servers),
            "database_count": db_total,
            "sku_distribution": dict(sku_dist),
            "status_distribution": dict(status_dist),
            "servers": server_rows,
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
        await cache_set(key, result)
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_dict(obj: Any) -> dict:
    f = getattr(obj, "as_dict", None)
    if callable(f):
        try:
            result = f()
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    if isinstance(obj, dict):
        return obj
    return {}


def _rg_from_id(rid: Optional[str]) -> Optional[str]:
    if not rid:
        return None
    parts = rid.split("/")
    try:
        idx = parts.index("resourceGroups")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return None


def _power_state_from_iv(instance_view: Any) -> Optional[str]:
    iv = _as_dict(instance_view)
    for s in iv.get("statuses") or []:
        code = s.get("code") or ""
        if code.startswith("PowerState/"):
            return code.split("/", 1)[1]
    return None


def _avg_metric(metrics_dict: dict, metric_name: str) -> Optional[float]:
    for m in metrics_dict.get("value") or []:
        name = (m.get("name") or {}).get("value")
        if name != metric_name:
            continue
        vals: list[float] = []
        for ts in m.get("timeseries") or []:
            for p in ts.get("data") or []:
                v = p.get("average")
                if v is not None:
                    vals.append(float(v))
        if vals:
            return sum(vals) / len(vals)
    return None


_ = Any
