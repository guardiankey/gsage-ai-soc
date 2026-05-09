"""gSage AI — Azure cost analytics and savings recommendations.

Wraps the Azure Cost Management ``query.usage`` API and the Azure
Advisor recommendations endpoint to produce SOC-friendly cost views:

- ``current_month_cost``      — Month-to-date cost (PreTaxCost) per service.
- ``cost_history``             — Daily cost trend over a configurable
                                window (default 30 days).
- ``cost_by_resource_group``   — Total cost grouped by resource group.
- ``top_resources_by_cost``    — Top-N resources by cost in the period.
- ``recommendations``          — Advisor cost recommendations + heuristic
                                checks from ``_recommendations.py``.
- ``potential_savings``        — Aggregate savings consolidation.

Per project decision #1, when the Cost Management API returns no data
(e.g. trial subs, missing entitlements), savings figures are reported
as ``None`` rather than synthesised from a static price list.

Permission: ``azure:read``.
"""

from __future__ import annotations

import logging
import time
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

_ACTIONS = frozenset({
    "current_month_cost",
    "cost_history",
    "cost_by_resource_group",
    "top_resources_by_cost",
    "recommendations",
    "potential_savings",
})

_DEFAULT_HISTORY_DAYS = 30
_MAX_HISTORY_DAYS = 365
_DEFAULT_TOP_N = 20


class _ParamError(Exception):
    pass


class AzureCostsTool(BaseTool):
    """Azure cost analytics + savings recommendations.

    All actions hit Azure Cost Management at the subscription scope.
    Heavy queries are cached for 5 minutes.

    Permission: ``azure:read``.
    """

    name: ClassVar[str] = "azure_costs"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Azure cost analytics: current MTD, history, top resources, "
        "RG breakdown, Advisor recommendations and consolidated savings."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "azure"
    permissions: ClassVar[list[str]] = ["azure:read"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 120
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "target_resource": "subscription_id",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which cost view to compute.",
            },
            "profile": {"type": "string"},
            "subscription_id": {
                "type": "string",
                "description": "Override the profile default subscription.",
            },
            "history_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_HISTORY_DAYS,
                "description": (
                    f"[cost_history] Window in days (default "
                    f"{_DEFAULT_HISTORY_DAYS}, max {_MAX_HISTORY_DAYS})."
                ),
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": (
                    f"[top_resources_by_cost] How many resources to "
                    f"return (default {_DEFAULT_TOP_N})."
                ),
            },
            "category": {
                "type": "string",
                "enum": ["Cost", "HighAvailability", "Performance",
                         "Security", "OperationalExcellence"],
                "description": (
                    "[recommendations] Advisor category filter (default Cost)."
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
        action = (params.get("action") or "").strip()
        if action not in _ACTIONS:
            return self._failure(
                "INVALID_PARAMS",
                f"action must be one of {sorted(_ACTIONS)}; got {action!r}.",
            )

        try:
            async with build_azure_client(config) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(client, params, agent_context)
        except _ParamError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INVALID_PARAMS", str(exc), execution_time_ms=elapsed
            )
        except AzureError as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                exc.code, str(exc), execution_time_ms=elapsed
            )
        except Exception as exc:
            log.exception("azure_costs(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={"action": action, **data}, execution_time_ms=elapsed
        )

    # ── Cache ──────────────────────────────────────────────────────────────

    async def _cached(
        self,
        agent_context: AgentContext,
        params: dict,
        sub_id: str,
        kind: str,
        filters: dict,
    ) -> tuple[Optional[dict], str]:
        org = str(getattr(agent_context, "org_id", "") or "")
        user = str(getattr(agent_context, "user_id", "") or "")
        profile = str(params.get("profile") or "default")
        key = build_cache_key(
            org_id=org,
            user_id=user,
            profile_id=profile,
            subscription_id=sub_id,
            kind=kind,
            filters=filters,
        )
        if params.get("force_refresh"):
            return None, key
        cached = await cache_get(key)
        if isinstance(cached, dict):
            return cached, key
        return None, key

    # ── Action handlers ─────────────────────────────────────────────────────

    async def _do_current_month_cost(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        cached, key = await self._cached(
            agent_context, params, sub_id, "mtd_cost", {}
        )
        if cached is not None:
            return {**cached, "cache_hit": True}
        rows, currency = await _query_usage(
            client, sub_id,
            timeframe="MonthToDate",
            grouping_name="ServiceName",
            grouping_type="Dimension",
        )
        total = sum(float(r.get("cost") or 0) for r in rows)
        result = {
            "subscription_id": sub_id,
            "timeframe": "MonthToDate",
            "currency": currency,
            "total_cost": round(total, 4),
            "by_service": rows,
        }
        await cache_set(key, result)
        return result

    async def _do_cost_history(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        days = min(
            int(params.get("history_days") or _DEFAULT_HISTORY_DAYS),
            _MAX_HISTORY_DAYS,
        )
        cached, key = await self._cached(
            agent_context, params, sub_id, "cost_history", {"days": days}
        )
        if cached is not None:
            return {**cached, "cache_hit": True}
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        rows, currency = await _query_usage(
            client, sub_id,
            timeframe="Custom",
            time_period=(start, end),
            granularity="Daily",
            grouping_name=None,
        )
        # Sort by date
        rows.sort(key=lambda r: r.get("date") or "")
        total = sum(float(r.get("cost") or 0) for r in rows)
        result = {
            "subscription_id": sub_id,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "currency": currency,
            "total_cost": round(total, 4),
            "daily": rows,
        }
        await cache_set(key, result)
        return result

    async def _do_cost_by_resource_group(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        cached, key = await self._cached(
            agent_context, params, sub_id, "cost_by_rg", {}
        )
        if cached is not None:
            return {**cached, "cache_hit": True}
        rows, currency = await _query_usage(
            client, sub_id,
            timeframe="MonthToDate",
            grouping_name="ResourceGroupName",
            grouping_type="Dimension",
        )
        rows.sort(key=lambda r: float(r.get("cost") or 0), reverse=True)
        total = sum(float(r.get("cost") or 0) for r in rows)
        result = {
            "subscription_id": sub_id,
            "timeframe": "MonthToDate",
            "currency": currency,
            "total_cost": round(total, 4),
            "by_resource_group": rows,
        }
        await cache_set(key, result)
        return result

    async def _do_top_resources_by_cost(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        top_n = int(params.get("top_n") or _DEFAULT_TOP_N)
        cached, key = await self._cached(
            agent_context, params, sub_id, "top_resources",
            {"top_n": top_n},
        )
        if cached is not None:
            return {**cached, "cache_hit": True}
        rows, currency = await _query_usage(
            client, sub_id,
            timeframe="MonthToDate",
            grouping_name="ResourceId",
            grouping_type="Dimension",
        )
        rows.sort(key=lambda r: float(r.get("cost") or 0), reverse=True)
        rows = rows[:top_n]
        result = {
            "subscription_id": sub_id,
            "timeframe": "MonthToDate",
            "currency": currency,
            "top_resources": rows,
        }
        await cache_set(key, result)
        return result

    async def _do_recommendations(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        category = (params.get("category") or "Cost").strip()
        cached, key = await self._cached(
            agent_context, params, sub_id, "recommendations",
            {"category": category},
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        ac = client.advisor(sub_id)
        items = await client.collect(
            ac.recommendations.list(), limit=1000
        )
        advisor_recs: list[dict] = []
        for r in items:
            d = _as_dict(r)
            props = d.get("properties") or d
            cat = (props.get("category") or "").strip()
            if category and cat and cat.lower() != category.lower():
                continue
            advisor_recs.append({
                "source": "advisor",
                "category": cat or category,
                "impact": props.get("impact"),
                "short_description": (
                    props.get("short_description") or {}
                ).get("problem"),
                "resource_id": props.get("resource_metadata", {}).get(
                    "resource_id"
                ) or d.get("id"),
                "potential_saving_estimate": _extract_advisor_savings(props),
                "currency": _extract_advisor_currency(props),
            })

        # Heuristic checks via inventory + metric calls (scoped to sub).
        heuristics = await _gather_heuristic_recommendations(client, sub_id)

        recs = advisor_recs + heuristics
        savings = consolidate_savings(recs)
        result = {
            "subscription_id": sub_id,
            "category": category,
            "advisor_count": len(advisor_recs),
            "heuristic_count": len(heuristics),
            "recommendations": recs,
            "savings": savings,
        }
        await cache_set(key, result)
        return result

    async def _do_potential_savings(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        # Reuse recommendations and just project the savings block.
        rec_data = await self._do_recommendations(
            client, params, agent_context
        )
        savings = rec_data.get("savings", {})
        return {
            "subscription_id": rec_data.get("subscription_id"),
            "savings": savings,
            "based_on": {
                "advisor_count": rec_data.get("advisor_count"),
                "heuristic_count": rec_data.get("heuristic_count"),
            },
        }


# ---------------------------------------------------------------------------
# Cost Management query helper
# ---------------------------------------------------------------------------


async def _query_usage(
    client: AzureClient,
    sub_id: str,
    *,
    timeframe: str,
    time_period: Optional[tuple[datetime, datetime]] = None,
    granularity: Optional[str] = None,
    grouping_name: Optional[str] = "ServiceName",
    grouping_type: str = "Dimension",
) -> tuple[list[dict], str]:
    """Run a Cost Management ``query.usage`` and return normalised rows.

    Returns ``(rows, currency)``. Each row is a dict whose key set
    depends on the grouping:
    - ServiceName grouping → ``{service, cost}``
    - ResourceGroupName grouping → ``{resource_group, cost}``
    - ResourceId grouping → ``{resource_id, cost}``
    - No grouping + Daily granularity → ``{date, cost}``
    """
    from azure.mgmt.costmanagement.models import (
        ExportType,
        QueryAggregation,
        QueryDataset,
        QueryDefinition,
        QueryGrouping,
        QueryTimePeriod,
        TimeframeType,
    )

    cc = client.cost()
    aggregation = {
        "totalCost": QueryAggregation(name="PreTaxCost", function="Sum"),
    }
    grouping = None
    if grouping_name:
        grouping = [QueryGrouping(type=grouping_type, name=grouping_name)]
    dataset = QueryDataset(
        granularity=granularity,
        aggregation=aggregation,
        grouping=grouping,
    )
    qdef_kwargs: dict[str, Any] = {
        "type": ExportType.ACTUAL_COST,
        "timeframe": TimeframeType[timeframe.upper()] if hasattr(
            TimeframeType, timeframe.upper()
        ) else timeframe,
        "dataset": dataset,
    }
    if time_period is not None:
        start, end = time_period
        qdef_kwargs["time_period"] = QueryTimePeriod(
            from_property=start, to=end
        )
    qdef = QueryDefinition(**qdef_kwargs)
    scope = f"/subscriptions/{sub_id}"

    try:
        result = await client.call(cc.query.usage(scope=scope, parameters=qdef))
    except AzureError:
        raise

    d = _as_dict(result)
    props = d.get("properties") or d
    columns = [c.get("name") for c in (props.get("columns") or [])]
    rows: list[dict] = []
    currency = "USD"
    for raw in props.get("rows") or []:
        rec = dict(zip(columns, raw))
        cost = rec.get("PreTaxCost") or rec.get("Cost") or 0
        currency = rec.get("Currency", currency) or currency
        if grouping_name == "ServiceName":
            rows.append({
                "service": rec.get("ServiceName"),
                "cost": float(cost),
            })
        elif grouping_name == "ResourceGroupName":
            rows.append({
                "resource_group": rec.get("ResourceGroupName"),
                "cost": float(cost),
            })
        elif grouping_name == "ResourceId":
            rows.append({
                "resource_id": rec.get("ResourceId"),
                "cost": float(cost),
            })
        else:
            # Time-based row when granularity=Daily
            date_val = rec.get("UsageDate")
            rows.append({
                "date": _normalize_date(date_val),
                "cost": float(cost),
            })
    return rows, currency


def _normalize_date(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val)
    # API returns int like 20240115
    if s.isdigit() and len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _extract_advisor_savings(props: dict) -> Optional[float]:
    extended = props.get("extended_properties") or {}
    for k in ("savingsAmount", "annualSavingsAmount"):
        v = extended.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _extract_advisor_currency(props: dict) -> Optional[str]:
    extended = props.get("extended_properties") or {}
    return extended.get("savingsCurrency") or extended.get("Currency")


# ---------------------------------------------------------------------------
# Heuristic recommendations (orphans + idle VMs + old snapshots)
# ---------------------------------------------------------------------------


async def _gather_heuristic_recommendations(
    client: AzureClient, sub_id: str
) -> list[dict]:
    recs: list[dict] = []
    cc = client.compute(sub_id)
    nc = client.network(sub_id)

    # Orphan disks
    try:
        disks = await client.collect(cc.disks.list(), limit=2000)
        for d in disks:
            dd = _as_dict(d)
            if not dd.get("managed_by"):
                rec = detect_orphan_disk(dd, cost_brl=None)
                if rec:
                    recs.append(rec)
    except AzureError as exc:
        log.debug("orphan disks scan failed: %s", exc)

    # Orphan public IPs
    try:
        pips = await client.collect(
            nc.public_ip_addresses.list_all(), limit=2000
        )
        for p in pips:
            pd = _as_dict(p)
            if not pd.get("ip_configuration"):
                rec = detect_orphan_public_ip(pd, cost_brl=None)
                if rec:
                    recs.append(rec)
    except AzureError as exc:
        log.debug("orphan public_ip scan failed: %s", exc)

    # Orphan NICs
    try:
        nics = await client.collect(
            nc.network_interfaces.list_all(), limit=2000
        )
        for n in nics:
            nd = _as_dict(n)
            if not nd.get("virtual_machine"):
                rec = detect_orphan_nic(nd)
                if rec:
                    recs.append(rec)
    except AzureError as exc:
        log.debug("orphan nics scan failed: %s", exc)

    # Old snapshots
    try:
        snaps = await client.collect(cc.snapshots.list(), limit=2000)
        for s in snaps:
            sd = _as_dict(s)
            rec = detect_old_snapshot(sd, cost_brl=None)
            if rec:
                recs.append(rec)
    except AzureError as exc:
        log.debug("snapshots scan failed: %s", exc)

    # Idle VMs (best-effort, only if a small number of VMs)
    try:
        vms = await client.collect(
            cc.virtual_machines.list_all(), limit=200
        )
        if len(vms) <= 50:
            mc = client.monitor(sub_id)
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=7)
            timespan = f"{start.isoformat()}/{end.isoformat()}"
            interval = timedelta(hours=1)
            for vm in vms:
                vd = _as_dict(vm)
                rid = vd.get("id")
                if not rid:
                    continue
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
                    rec = detect_idle_vm(
                        vd, cpu_avg_pct=cpu_avg, net_bytes_avg=net_avg,
                        cost_brl=None,
                    )
                    if rec:
                        recs.append(rec)
                except AzureError as exc:
                    log.debug("idle metrics for %s failed: %s", rid, exc)
    except AzureError as exc:
        log.debug("idle VMs scan failed: %s", exc)

    return recs


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


_ = Any
