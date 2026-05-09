"""gSage AI — Azure Monitor metric queries.

Wraps the Azure Monitor ``metrics.list`` REST API in an action-dispatcher
tool that returns standardised time-series payloads. Each action targets
a specific Azure resource type and supplies the right metric set:

- ``vm_metrics``           — Percentage CPU, Available Memory Bytes,
                             Network In/Out Total, Disk Read/Write Ops/Sec.
- ``disk_metrics``          — Composite Disk Read/Write Operations/Bytes per Sec.
- ``app_service_metrics``   — CpuTime, MemoryWorkingSet, Requests, Http5xx.
- ``sql_metrics``           — cpu_percent, dtu_consumption_percent,
                             storage_percent.
- ``uptime``                — Power state changes derived from the
                             Activity Log (Microsoft.Compute/virtualMachines).

All actions accept ``timespan_hours``, ``interval`` and ``aggregation``
parameters and return a flat ``{series, summary}`` payload. Heavy
queries are cached for ``CACHE_TTL_SECONDS`` (default 300s).

Permission: ``azure:read``.
"""

from __future__ import annotations

import logging
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Optional, Sequence

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
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = frozenset({
    "vm_metrics",
    "disk_metrics",
    "app_service_metrics",
    "sql_metrics",
    "uptime",
})

_INTERVAL_PRESETS = ("PT1M", "PT5M", "PT15M", "PT1H", "PT1D")
_AGGREGATIONS = ("Average", "Total", "Maximum", "Minimum", "Count")
_DEFAULT_TIMESPAN_HOURS = 24
_MAX_TIMESPAN_HOURS = 30 * 24  # 30 days

_VM_METRICS = (
    "Percentage CPU",
    "Available Memory Bytes",
    "Network In Total",
    "Network Out Total",
    "Disk Read Operations/Sec",
    "Disk Write Operations/Sec",
)
_DISK_METRICS = (
    "Composite Disk Read Operations/sec",
    "Composite Disk Write Operations/sec",
    "Composite Disk Read Bytes/sec",
    "Composite Disk Write Bytes/sec",
)
_APP_SERVICE_METRICS = (
    "CpuTime",
    "MemoryWorkingSet",
    "Requests",
    "Http5xx",
)
_SQL_METRICS = (
    "cpu_percent",
    "dtu_consumption_percent",
    "storage_percent",
)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class _ParamError(Exception):
    pass


class AzureMetricsTool(BaseTool):
    """Time-series metric queries against Azure Monitor.

    Use one ``action`` per call. Returns a standardised
    ``{series, summary}`` payload; results are cached for 5 minutes.

    Permission: ``azure:read``.
    """

    name: ClassVar[str] = "azure_metrics"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Azure Monitor time-series for VM/disk/AppService/SQL plus VM "
        "uptime from Activity Log."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "azure"
    permissions: ClassVar[list[str]] = ["azure:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 60
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "target_resource": "resource_group",
        "target_entities": "name",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which metric query to run.",
            },
            "profile": {
                "type": "string",
                "description": "GSageToolConfig profile to use.",
            },
            "subscription_id": {
                "type": "string",
                "description": "Override the profile default subscription.",
            },
            "resource_group": {
                "type": "string",
                "description": "Resource group of the target resource.",
            },
            "name": {
                "type": "string",
                "description": (
                    "Resource name (vm_metrics→VM name, disk_metrics→disk "
                    "name, app_service_metrics→site name, sql_metrics→DB "
                    "name, uptime→VM name)."
                ),
            },
            "server_name": {
                "type": "string",
                "description": (
                    "[sql_metrics] SQL server name hosting the database."
                ),
            },
            "resource_id": {
                "type": "string",
                "description": (
                    "Optional full resource ID. When provided, "
                    "resource_group/name are not required."
                ),
            },
            "metricnames": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Override the default metric set for this action."
                ),
            },
            "timespan_hours": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_TIMESPAN_HOURS,
                "description": (
                    f"Look-back window in hours (default "
                    f"{_DEFAULT_TIMESPAN_HOURS}, max {_MAX_TIMESPAN_HOURS})."
                ),
            },
            "interval": {
                "type": "string",
                "enum": list(_INTERVAL_PRESETS),
                "description": (
                    "Aggregation interval (ISO 8601 duration). Default "
                    "PT5M for ≤24h, PT1H for ≤7d, PT1D for ≤30d."
                ),
            },
            "aggregation": {
                "type": "string",
                "enum": list(_AGGREGATIONS),
                "description": "Metric aggregation (default Average).",
            },
            "force_refresh": {
                "type": "boolean",
                "description": (
                    "Bypass the Redis cache (TTL "
                    f"{CACHE_TTL_SECONDS}s)."
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
            log.exception("azure_metrics(%s): unexpected error", action)
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

    async def _do_vm_metrics(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        rid = self._build_resource_id(
            params, sub_id,
            "Microsoft.Compute", "virtualMachines",
        )
        names = list(params.get("metricnames") or _VM_METRICS)
        return await self._run_metrics(
            client, sub_id, rid, names, params,
            agent_context, kind="vm_metrics",
        )

    async def _do_disk_metrics(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        rid = self._build_resource_id(
            params, sub_id,
            "Microsoft.Compute", "disks",
        )
        names = list(params.get("metricnames") or _DISK_METRICS)
        return await self._run_metrics(
            client, sub_id, rid, names, params,
            agent_context, kind="disk_metrics",
        )

    async def _do_app_service_metrics(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        rid = self._build_resource_id(
            params, sub_id,
            "Microsoft.Web", "sites",
        )
        names = list(params.get("metricnames") or _APP_SERVICE_METRICS)
        return await self._run_metrics(
            client, sub_id, rid, names, params,
            agent_context, kind="app_service_metrics",
        )

    async def _do_sql_metrics(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        rid = (params.get("resource_id") or "").strip()
        if not rid:
            rg = _require(params, "resource_group")
            server = _require(params, "server_name")
            db = _require(params, "name")
            rid = (
                f"/subscriptions/{sub_id}/resourceGroups/{rg}/providers/"
                f"Microsoft.Sql/servers/{server}/databases/{db}"
            )
        names = list(params.get("metricnames") or _SQL_METRICS)
        return await self._run_metrics(
            client, sub_id, rid, names, params,
            agent_context, kind="sql_metrics",
        )

    async def _do_uptime(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        rid = self._build_resource_id(
            params, sub_id,
            "Microsoft.Compute", "virtualMachines",
        )
        timespan_hours = int(params.get("timespan_hours") or 7 * 24)
        timespan_hours = min(timespan_hours, _MAX_TIMESPAN_HOURS)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=timespan_hours)
        filters = {
            "rid": rid,
            "timespan_hours": timespan_hours,
        }
        cached, key = await self._cached(
            agent_context, params, sub_id, "uptime", filters
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        # Activity log filter on the VM resource id.
        f = (
            f"eventTimestamp ge '{start.isoformat()}' "
            f"and eventTimestamp le '{end.isoformat()}' "
            f"and resourceUri eq '{rid}'"
        )
        mc = client.monitor(sub_id)
        items = await client.collect(
            mc.activity_logs.list(filter=f), limit=2000
        )
        events: list[dict] = []
        for ev in items:
            d = _as_dict(ev)
            op = (d.get("operation_name") or {}).get("value") or ""
            status = (d.get("status") or {}).get("value")
            if op.lower().endswith(
                (
                    "/start/action",
                    "/poweroff/action",
                    "/deallocate/action",
                    "/restart/action",
                )
            ):
                events.append({
                    "ts": d.get("event_timestamp"),
                    "operation": op,
                    "status": status,
                    "caller": d.get("caller"),
                })
        events.sort(key=lambda e: e.get("ts") or "")

        # Estimate uptime ratio: assume VM was running between consecutive
        # 'start' / 'deallocate' events (best-effort).
        running_seconds = 0
        cur_start: Optional[datetime] = None
        for ev in events:
            ts = ev.get("ts")
            if not ts:
                continue
            try:
                ts_dt = datetime.fromisoformat(
                    str(ts).replace("Z", "+00:00")
                )
            except Exception:
                continue
            op = (ev.get("operation") or "").lower()
            if "start" in op and ev.get("status") == "Succeeded":
                cur_start = ts_dt
            elif (
                ("deallocate" in op or "poweroff" in op)
                and ev.get("status") == "Succeeded"
                and cur_start is not None
            ):
                running_seconds += int((ts_dt - cur_start).total_seconds())
                cur_start = None
        # If still running at end of window, count partial uptime.
        if cur_start is not None:
            running_seconds += int((end - cur_start).total_seconds())
        total_seconds = int((end - start).total_seconds())
        uptime_ratio = (
            running_seconds / total_seconds if total_seconds > 0 else None
        )
        result = {
            "resource_id": rid,
            "timespan": {"start": start.isoformat(), "end": end.isoformat()},
            "events": events,
            "summary": {
                "event_count": len(events),
                "running_seconds": running_seconds,
                "total_seconds": total_seconds,
                "uptime_ratio": uptime_ratio,
            },
        }
        await cache_set(key, result)
        return result

    # ── Common metrics runner ───────────────────────────────────────────────

    async def _run_metrics(
        self,
        client: AzureClient,
        sub_id: str,
        rid: str,
        metricnames: Sequence[str],
        params: dict,
        agent_context: AgentContext,
        *,
        kind: str,
    ) -> dict:
        timespan_hours = int(
            params.get("timespan_hours") or _DEFAULT_TIMESPAN_HOURS
        )
        timespan_hours = min(timespan_hours, _MAX_TIMESPAN_HOURS)
        interval = (params.get("interval") or "").strip() or _pick_interval(
            timespan_hours
        )
        aggregation = (
            params.get("aggregation") or "Average"
        ).strip()
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=timespan_hours)
        timespan = f"{start.isoformat()}/{end.isoformat()}"

        filters = {
            "rid": rid,
            "metricnames": tuple(sorted(metricnames)),
            "timespan_hours": timespan_hours,
            "interval": interval,
            "aggregation": aggregation,
        }
        cached, key = await self._cached(
            agent_context, params, sub_id, kind, filters
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        mc = client.monitor(sub_id)
        try:
            res = await client.call(mc.metrics.list(
                resource_uri=rid,
                timespan=timespan,
                interval=_iso_to_timedelta(interval),
                metricnames=",".join(metricnames),
                aggregation=aggregation,
            ))
        except AzureError:
            raise

        d = _as_dict(res)
        series = []
        for m in d.get("value") or []:
            mname = (m.get("name") or {}).get("value") or ""
            unit = m.get("unit")
            timeseries = m.get("timeseries") or []
            points: list[dict] = []
            for ts in timeseries:
                for p in ts.get("data") or []:
                    val = p.get(_aggregation_field(aggregation))
                    if val is None:
                        continue
                    points.append({"ts": p.get("time_stamp"), "value": val})
            series.append({
                "metric": mname,
                "unit": unit,
                "aggregation": aggregation,
                "points": points,
                "summary": _summary(points),
            })
        result = {
            "resource_id": rid,
            "timespan": {"start": start.isoformat(), "end": end.isoformat()},
            "interval": interval,
            "aggregation": aggregation,
            "series": series,
            "summary": {
                "metric_count": len(series),
                "total_points": sum(len(s.get("points") or []) for s in series),
            },
        }
        await cache_set(key, result)
        return result

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_resource_id(
        params: dict, sub_id: str, provider: str, kind: str
    ) -> str:
        rid = (params.get("resource_id") or "").strip()
        if rid:
            return rid
        rg = _require(params, "resource_group")
        name = _require(params, "name")
        return (
            f"/subscriptions/{sub_id}/resourceGroups/{rg}/providers/"
            f"{provider}/{kind}/{name}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _aggregation_field(agg: str) -> str:
    return {
        "Average": "average",
        "Total": "total",
        "Maximum": "maximum",
        "Minimum": "minimum",
        "Count": "count",
    }.get(agg, "average")


def _pick_interval(timespan_hours: int) -> str:
    if timespan_hours <= 1:
        return "PT1M"
    if timespan_hours <= 24:
        return "PT5M"
    if timespan_hours <= 7 * 24:
        return "PT1H"
    return "PT1D"


_ISO_DURATION_RE = re.compile(
    r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$|^P(\d+)D$"
)


def _iso_to_timedelta(s: str) -> timedelta:
    """Convert a small subset of ISO 8601 durations to ``timedelta``.

    Supports ``PT1M``, ``PT5M``, ``PT15M``, ``PT1H``, ``P1D``/``PT1D``.
    Defaults to 5 minutes for unrecognised input.
    """
    if not s:
        return timedelta(minutes=5)
    s = s.strip().upper()
    # Handle PT1D explicitly (Azure accepts it as 1 day)
    if s == "PT1D":
        return timedelta(days=1)
    m = _ISO_DURATION_RE.match(s)
    if not m:
        return timedelta(minutes=5)
    h, mm, ss, dd = m.groups()
    if dd:
        return timedelta(days=int(dd))
    return timedelta(
        hours=int(h or 0),
        minutes=int(mm or 0),
        seconds=int(ss or 0),
    )


def _summary(points: list[dict]) -> dict:
    if not points:
        return {"min": None, "max": None, "avg": None, "p95": None, "count": 0}
    values = [float(p["value"]) for p in points if p.get("value") is not None]
    if not values:
        return {"min": None, "max": None, "avg": None, "p95": None, "count": 0}
    values_sorted = sorted(values)
    n = len(values_sorted)
    p95_idx = max(0, int(round(0.95 * (n - 1))))
    return {
        "min": values_sorted[0],
        "max": values_sorted[-1],
        "avg": round(statistics.fmean(values_sorted), 4),
        "p95": values_sorted[p95_idx],
        "count": n,
    }


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


def _require(params: dict, field: str) -> str:
    val = params.get(field)
    if isinstance(val, str):
        val = val.strip()
    if not val:
        raise _ParamError(f"'{field}' is required for this action.")
    return str(val)


_ = Any
