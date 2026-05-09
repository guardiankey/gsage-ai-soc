"""gSage AI — Azure read-only inventory and orphans tool.

Exposes an ``action`` enum dispatcher covering the most common
read-only inventory queries Tier 1/2 analysts need when triaging cost,
capacity or compliance questions on an Azure subscription:

- ``list_subscriptions``, ``list_resource_groups``
- ``list_vms`` (with optional ``power_state`` enrichment)
- ``list_disks``, ``list_public_ips``, ``list_nics``
- ``list_sql_servers``, ``list_sql_databases``
- ``list_app_services``, ``list_aks_clusters``, ``list_storage_accounts``
- ``list_snapshots`` (with optional ``older_than_days`` filter)
- ``list_orphans`` (composite: orphan disks/IPs/NICs + old snapshots)
- ``describe_resource`` (single resource ID lookup)

Tabular results are run through the shared ``result_export`` pipeline:
when over 100 rows, a CSV artifact is auto-generated and only the first
100 rows are inlined for the agent. ``export_csv=true`` forces CSV for
any size.

Permission: ``azure:read``. Multi-subscription via
``params.subscription_id`` (override of the profile default).
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
from src.mcp_server.tools.result_export import (
    AGENT_PREVIEW_ROWS,
    build_agent_payload,
)
from src.shared.security.context import AgentContext

log = logging.getLogger(__name__)

_ACTIONS = frozenset({
    "list_subscriptions",
    "list_resource_groups",
    "list_vms",
    "list_disks",
    "list_public_ips",
    "list_nics",
    "list_sql_servers",
    "list_sql_databases",
    "list_app_services",
    "list_aks_clusters",
    "list_storage_accounts",
    "list_snapshots",
    "list_orphans",
    "describe_resource",
})

_DEFAULT_RESULTS = 100
_MAX_RESULTS = 1000


# ---------------------------------------------------------------------------
# Slim views
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


def _slim_subscription(sub: Any) -> dict:
    d = _as_dict(sub)
    return {
        "subscription_id": d.get("subscription_id"),
        "display_name": d.get("display_name"),
        "state": d.get("state"),
        "tenant_id": d.get("tenant_id"),
    }


def _slim_resource_group(rg: Any) -> dict:
    d = _as_dict(rg)
    return {
        "name": d.get("name"),
        "location": d.get("location"),
        "id": d.get("id"),
        "provisioning_state": (d.get("properties") or {}).get(
            "provisioning_state"
        ),
        "tags": d.get("tags") or {},
    }


def _slim_vm(vm: Any, power_state: Optional[str] = None) -> dict:
    d = _as_dict(vm)
    storage = d.get("storage_profile") or {}
    os_disk = (storage.get("os_disk") or {}).get("name")
    nics = ((d.get("network_profile") or {}).get("network_interfaces") or [])
    return {
        "name": d.get("name"),
        "id": d.get("id"),
        "resource_group": _rg_from_id(d.get("id")),
        "location": d.get("location"),
        "vm_size": ((d.get("hardware_profile") or {}).get("vm_size")),
        "os_type": ((storage.get("os_disk") or {}).get("os_type")),
        "image": (
            (storage.get("image_reference") or {}).get("publisher"),
            (storage.get("image_reference") or {}).get("offer"),
            (storage.get("image_reference") or {}).get("sku"),
        ),
        "os_disk": os_disk,
        "data_disk_count": len(storage.get("data_disks") or []),
        "primary_nic": (nics[0].get("id") if nics else None),
        "power_state": power_state,
        "tags": d.get("tags") or {},
        "zones": d.get("zones") or [],
    }


def _slim_disk(disk: Any) -> dict:
    d = _as_dict(disk)
    return {
        "name": d.get("name"),
        "id": d.get("id"),
        "resource_group": _rg_from_id(d.get("id")),
        "location": d.get("location"),
        "disk_size_gb": d.get("disk_size_gb"),
        "sku": (d.get("sku") or {}).get("name"),
        "tier": (d.get("sku") or {}).get("tier"),
        "disk_state": d.get("disk_state"),
        "managed_by": d.get("managed_by"),
        "time_created": d.get("time_created"),
        "tags": d.get("tags") or {},
    }


def _slim_public_ip(pip: Any) -> dict:
    d = _as_dict(pip)
    cfg = d.get("ip_configuration")
    return {
        "name": d.get("name"),
        "id": d.get("id"),
        "resource_group": _rg_from_id(d.get("id")),
        "location": d.get("location"),
        "ip_address": d.get("ip_address"),
        "allocation_method": d.get("public_ip_allocation_method"),
        "sku": (d.get("sku") or {}).get("name"),
        "associated_to": (cfg or {}).get("id"),
        "tags": d.get("tags") or {},
    }


def _slim_nic(nic: Any) -> dict:
    d = _as_dict(nic)
    vm = d.get("virtual_machine")
    ip_cfgs = d.get("ip_configurations") or []
    return {
        "name": d.get("name"),
        "id": d.get("id"),
        "resource_group": _rg_from_id(d.get("id")),
        "location": d.get("location"),
        "virtual_machine": (vm or {}).get("id") if vm else None,
        "primary_private_ip": (
            ip_cfgs[0].get("private_ip_address") if ip_cfgs else None
        ),
        "ip_configuration_count": len(ip_cfgs),
        "tags": d.get("tags") or {},
    }


def _slim_sql_server(srv: Any) -> dict:
    d = _as_dict(srv)
    return {
        "name": d.get("name"),
        "id": d.get("id"),
        "resource_group": _rg_from_id(d.get("id")),
        "location": d.get("location"),
        "version": d.get("version"),
        "fully_qualified_domain_name": d.get("fully_qualified_domain_name"),
        "state": d.get("state"),
        "public_network_access": d.get("public_network_access"),
        "tags": d.get("tags") or {},
    }


def _slim_sql_db(db: Any, server_name: Optional[str] = None) -> dict:
    d = _as_dict(db)
    return {
        "name": d.get("name"),
        "id": d.get("id"),
        "server": server_name,
        "resource_group": _rg_from_id(d.get("id")),
        "location": d.get("location"),
        "sku": (d.get("sku") or {}).get("name"),
        "tier": (d.get("sku") or {}).get("tier"),
        "status": d.get("status"),
        "max_size_bytes": d.get("max_size_bytes"),
        "zone_redundant": d.get("zone_redundant"),
        "creation_date": d.get("creation_date"),
    }


def _slim_app_service(app: Any) -> dict:
    d = _as_dict(app)
    return {
        "name": d.get("name"),
        "id": d.get("id"),
        "resource_group": _rg_from_id(d.get("id")),
        "location": d.get("location"),
        "kind": d.get("kind"),
        "state": d.get("state"),
        "default_host_name": d.get("default_host_name"),
        "https_only": d.get("https_only"),
        "server_farm_id": d.get("server_farm_id"),
        "tags": d.get("tags") or {},
    }


def _slim_aks(cluster: Any) -> dict:
    d = _as_dict(cluster)
    pools = d.get("agent_pool_profiles") or []
    return {
        "name": d.get("name"),
        "id": d.get("id"),
        "resource_group": _rg_from_id(d.get("id")),
        "location": d.get("location"),
        "kubernetes_version": d.get("kubernetes_version"),
        "provisioning_state": d.get("provisioning_state"),
        "power_state": (d.get("power_state") or {}).get("code"),
        "node_count_total": sum(int(p.get("count") or 0) for p in pools),
        "node_pools": [
            {
                "name": p.get("name"),
                "vm_size": p.get("vm_size"),
                "count": p.get("count"),
                "mode": p.get("mode"),
            }
            for p in pools
        ],
        "sku_tier": (d.get("sku") or {}).get("tier"),
        "rbac_enabled": d.get("enable_rbac"),
        "tags": d.get("tags") or {},
    }


def _slim_storage(acc: Any) -> dict:
    d = _as_dict(acc)
    return {
        "name": d.get("name"),
        "id": d.get("id"),
        "resource_group": _rg_from_id(d.get("id")),
        "location": d.get("location"),
        "kind": d.get("kind"),
        "sku": (d.get("sku") or {}).get("name"),
        "access_tier": d.get("access_tier"),
        "https_traffic_only": d.get("enable_https_traffic_only"),
        "creation_time": d.get("creation_time"),
        "tags": d.get("tags") or {},
    }


def _slim_snapshot(snap: Any) -> dict:
    d = _as_dict(snap)
    return {
        "name": d.get("name"),
        "id": d.get("id"),
        "resource_group": _rg_from_id(d.get("id")),
        "location": d.get("location"),
        "disk_size_gb": d.get("disk_size_gb"),
        "sku": (d.get("sku") or {}).get("name"),
        "incremental": d.get("incremental"),
        "time_created": d.get("time_created"),
        "source_resource_id": (d.get("creation_data") or {}).get(
            "source_resource_id"
        ),
        "tags": d.get("tags") or {},
    }


def _rg_from_id(rid: Optional[str]) -> Optional[str]:
    """Extract the resource group name from a resource ID."""
    if not rid:
        return None
    parts = rid.split("/")
    try:
        idx = parts.index("resourceGroups")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return None


def _matches_tag(tags: Optional[dict], tag_filter: Optional[str]) -> bool:
    """Match a 'key=value' or 'key' tag filter."""
    if not tag_filter:
        return True
    if not tags:
        return False
    if "=" in tag_filter:
        k, v = tag_filter.split("=", 1)
        return tags.get(k.strip()) == v.strip()
    return tag_filter in tags


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class _ParamError(Exception):
    pass


class AzureInventoryTool(BaseTool):
    """Read-only Azure inventory + orphan detection.

    Use one ``action`` per call. Tabular results auto-export as CSV when
    over 100 rows; the agent receives only the first 100 rows plus a
    download link.

    Permission: ``azure:read``.
    """

    name: ClassVar[str] = "azure_inventory"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Read-only Azure inventory: subscriptions, RGs, VMs, disks, IPs, "
        "NICs, SQL, App Services, AKS, storage, snapshots, orphans. "
        "Auto-CSV on >100 rows."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "azure"
    permissions: ClassVar[list[str]] = ["azure:read"]
    rate_limit_per_minute: ClassVar[int] = 60
    timeout_seconds: ClassVar[int] = 120
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
                "description": "Which read-only inventory query to run.",
            },
            "profile": {
                "type": "string",
                "description": (
                    "GSageToolConfig profile (Service Principal) to use. "
                    "Omit for the 'default' profile."
                ),
            },
            "subscription_id": {
                "type": "string",
                "description": (
                    "Override the profile default subscription. "
                    "Ignored for 'list_subscriptions'."
                ),
            },
            "resource_group": {
                "type": "string",
                "description": (
                    "Filter by resource group. Optional for most list_* "
                    "actions; required for some describe_* lookups."
                ),
            },
            "tag": {
                "type": "string",
                "description": (
                    "Filter resources by tag. Accepts either 'key' (any "
                    "value) or 'key=value'."
                ),
            },
            "power_state": {
                "type": "string",
                "enum": [
                    "running", "stopped", "deallocated", "starting",
                    "stopping", "deallocating", "unknown",
                ],
                "description": (
                    "[list_vms] Filter VMs by power state (requires an "
                    "extra instance_view call per VM)."
                ),
            },
            "include_power_state": {
                "type": "boolean",
                "description": (
                    "[list_vms] Enrich each VM with its power state "
                    "(default false; adds 1 API call per VM)."
                ),
            },
            "attached": {
                "type": "boolean",
                "description": (
                    "[list_disks, list_public_ips] Filter by attachment "
                    "state. true = attached only; false = orphan only; "
                    "omit for both."
                ),
            },
            "older_than_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3650,
                "description": (
                    "[list_snapshots] Only include snapshots older than N "
                    "days."
                ),
            },
            "server_name": {
                "type": "string",
                "description": (
                    "[list_sql_databases] SQL server name. Required."
                ),
            },
            "resource_id": {
                "type": "string",
                "description": (
                    "[describe_resource] Full Azure resource ID "
                    "(/subscriptions/.../providers/...)."
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
                    "Bypass the Redis cache for this call (still writes "
                    f"the fresh value back). Cache TTL is "
                    f"{CACHE_TTL_SECONDS}s."
                ),
            },
            "export_csv": {
                "type": "boolean",
                "description": (
                    "Force CSV artifact even for small results. CSV is "
                    f"generated automatically when the result exceeds "
                    f"{AGENT_PREVIEW_ROWS} rows regardless."
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
        max_results = min(
            int(params.get("max_results") or _DEFAULT_RESULTS), _MAX_RESULTS
        )

        try:
            async with build_azure_client(config) as client:
                handler = getattr(self, f"_do_{action}")
                data = await handler(
                    client, params, agent_context, max_results
                )
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
            log.exception("azure_inventory(%s): unexpected error", action)
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

    def _cache_filters(self, params: dict, *extra_keys: str) -> dict:
        """Build a stable filter dict for the cache key."""
        f = {
            "resource_group": params.get("resource_group") or "",
            "tag": params.get("tag") or "",
        }
        for k in extra_keys:
            f[k] = params.get(k)
        return f

    async def _maybe_cached_rows(
        self,
        agent_context: AgentContext,
        client: AzureClient,
        sub_id: str,
        kind: str,
        params: dict,
        filters: dict,
    ) -> tuple[Optional[list[dict]], str]:
        """Look up cached rows; return (rows or None, cache_key)."""
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
        if isinstance(cached, list):
            return cached, key
        return None, key

    # ── Action handlers ─────────────────────────────────────────────────────

    async def _do_list_subscriptions(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        # Subscription listing is global to the SP; cache key uses sub_id="*"
        rows_cached, key = await self._maybe_cached_rows(
            agent_context, client, "*", "subscriptions", params, {}
        )
        if rows_cached is not None:
            rows = rows_cached[:max_results]
            return await self._tabular(
                agent_context, "list_subscriptions", rows, params,
                cache_hit=True,
            )
        sub_client = client.subscriptions()
        items = await client.collect(
            sub_client.subscriptions.list(), limit=max_results
        )
        rows = [_slim_subscription(s) for s in items]
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, "list_subscriptions", rows, params
        )

    async def _do_list_resource_groups(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        rows_cached, key = await self._maybe_cached_rows(
            agent_context, client, sub_id, "resource_groups", params,
            self._cache_filters(params),
        )
        if rows_cached is not None:
            return await self._tabular(
                agent_context, "list_resource_groups",
                rows_cached[:max_results], params, cache_hit=True,
            )
        rg_client = client.resource(sub_id)
        items = await client.collect(
            rg_client.resource_groups.list(), limit=_MAX_RESULTS
        )
        rows = [_slim_resource_group(rg) for rg in items]
        if tag := params.get("tag"):
            rows = [r for r in rows if _matches_tag(r.get("tags"), tag)]
        rows = rows[:max_results]
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, "list_resource_groups", rows, params
        )

    async def _do_list_vms(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        include_ps = bool(params.get("include_power_state")) or bool(
            params.get("power_state")
        )
        filters = {
            **self._cache_filters(params),
            "include_power_state": include_ps,
            "power_state": params.get("power_state") or "",
        }
        rows_cached, key = await self._maybe_cached_rows(
            agent_context, client, sub_id, "vms", params, filters
        )
        if rows_cached is not None:
            return await self._tabular(
                agent_context, "list_vms",
                rows_cached[:max_results], params, cache_hit=True,
            )
        cc = client.compute(sub_id)
        rg = (params.get("resource_group") or "").strip()
        if rg:
            iterator = cc.virtual_machines.list(rg)
        else:
            iterator = cc.virtual_machines.list_all()
        items = await client.collect(iterator, limit=_MAX_RESULTS)
        rows: list[dict] = []
        tag = params.get("tag")
        for vm in items:
            ps: Optional[str] = None
            d = _as_dict(vm)
            if include_ps and d.get("name"):
                vm_rg = _rg_from_id(d.get("id"))
                if vm_rg:
                    try:
                        iv = await client.call(
                            cc.virtual_machines.instance_view(  # type: ignore[attr-defined]
                                vm_rg, d["name"]
                            )
                        )
                        ps = _power_state_from_iv(iv)
                    except AzureError as exc:
                        log.debug(
                            "instance_view(%s) failed: %s", d.get("name"), exc
                        )
            row = _slim_vm(vm, ps)
            if tag and not _matches_tag(row.get("tags"), tag):
                continue
            if (req_ps := params.get("power_state")) and ps and ps != req_ps:
                continue
            rows.append(row)
        rows = rows[:max_results]
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, "list_vms", rows, params
        )

    async def _do_list_disks(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        attached = params.get("attached")
        filters = {**self._cache_filters(params), "attached": attached}
        rows_cached, key = await self._maybe_cached_rows(
            agent_context, client, sub_id, "disks", params, filters
        )
        if rows_cached is not None:
            return await self._tabular(
                agent_context, "list_disks",
                rows_cached[:max_results], params, cache_hit=True,
            )
        cc = client.compute(sub_id)
        rg = (params.get("resource_group") or "").strip()
        if rg:
            iterator = cc.disks.list_by_resource_group(rg)
        else:
            iterator = cc.disks.list()
        items = await client.collect(iterator, limit=_MAX_RESULTS)
        rows = [_slim_disk(d) for d in items]
        if attached is True:
            rows = [r for r in rows if r.get("managed_by")]
        elif attached is False:
            rows = [r for r in rows if not r.get("managed_by")]
        if tag := params.get("tag"):
            rows = [r for r in rows if _matches_tag(r.get("tags"), tag)]
        rows = rows[:max_results]
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, "list_disks", rows, params
        )

    async def _do_list_public_ips(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        attached = params.get("attached")
        filters = {**self._cache_filters(params), "attached": attached}
        rows_cached, key = await self._maybe_cached_rows(
            agent_context, client, sub_id, "public_ips", params, filters
        )
        if rows_cached is not None:
            return await self._tabular(
                agent_context, "list_public_ips",
                rows_cached[:max_results], params, cache_hit=True,
            )
        nc = client.network(sub_id)
        rg = (params.get("resource_group") or "").strip()
        if rg:
            iterator = nc.public_ip_addresses.list(rg)
        else:
            iterator = nc.public_ip_addresses.list_all()
        items = await client.collect(iterator, limit=_MAX_RESULTS)
        rows = [_slim_public_ip(p) for p in items]
        if attached is True:
            rows = [r for r in rows if r.get("associated_to")]
        elif attached is False:
            rows = [r for r in rows if not r.get("associated_to")]
        if tag := params.get("tag"):
            rows = [r for r in rows if _matches_tag(r.get("tags"), tag)]
        rows = rows[:max_results]
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, "list_public_ips", rows, params
        )

    async def _do_list_nics(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        rows_cached, key = await self._maybe_cached_rows(
            agent_context, client, sub_id, "nics", params,
            self._cache_filters(params),
        )
        if rows_cached is not None:
            return await self._tabular(
                agent_context, "list_nics",
                rows_cached[:max_results], params, cache_hit=True,
            )
        nc = client.network(sub_id)
        rg = (params.get("resource_group") or "").strip()
        if rg:
            iterator = nc.network_interfaces.list(rg)
        else:
            iterator = nc.network_interfaces.list_all()
        items = await client.collect(iterator, limit=_MAX_RESULTS)
        rows = [_slim_nic(n) for n in items]
        if tag := params.get("tag"):
            rows = [r for r in rows if _matches_tag(r.get("tags"), tag)]
        rows = rows[:max_results]
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, "list_nics", rows, params
        )

    async def _do_list_sql_servers(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        rows_cached, key = await self._maybe_cached_rows(
            agent_context, client, sub_id, "sql_servers", params,
            self._cache_filters(params),
        )
        if rows_cached is not None:
            return await self._tabular(
                agent_context, "list_sql_servers",
                rows_cached[:max_results], params, cache_hit=True,
            )
        sc = client.sql(sub_id)
        rg = (params.get("resource_group") or "").strip()
        if rg:
            iterator = sc.servers.list_by_resource_group(rg)
        else:
            iterator = sc.servers.list()
        items = await client.collect(iterator, limit=_MAX_RESULTS)
        rows = [_slim_sql_server(s) for s in items]
        if tag := params.get("tag"):
            rows = [r for r in rows if _matches_tag(r.get("tags"), tag)]
        rows = rows[:max_results]
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, "list_sql_servers", rows, params
        )

    async def _do_list_sql_databases(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        rg = _require(params, "resource_group")
        server = _require(params, "server_name")
        filters = {"resource_group": rg, "server_name": server}
        rows_cached, key = await self._maybe_cached_rows(
            agent_context, client, sub_id, "sql_databases", params, filters
        )
        if rows_cached is not None:
            return await self._tabular(
                agent_context, "list_sql_databases",
                rows_cached[:max_results], params, cache_hit=True,
            )
        sc = client.sql(sub_id)
        items = await client.collect(
            sc.databases.list_by_server(rg, server), limit=_MAX_RESULTS
        )
        rows = [_slim_sql_db(db, server_name=server) for db in items]
        rows = rows[:max_results]
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, "list_sql_databases", rows, params
        )

    async def _do_list_app_services(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        rows_cached, key = await self._maybe_cached_rows(
            agent_context, client, sub_id, "app_services", params,
            self._cache_filters(params),
        )
        if rows_cached is not None:
            return await self._tabular(
                agent_context, "list_app_services",
                rows_cached[:max_results], params, cache_hit=True,
            )
        wc = client.web(sub_id)
        rg = (params.get("resource_group") or "").strip()
        if rg:
            iterator = wc.web_apps.list_by_resource_group(rg)
        else:
            iterator = wc.web_apps.list()
        items = await client.collect(iterator, limit=_MAX_RESULTS)
        rows = [_slim_app_service(a) for a in items]
        if tag := params.get("tag"):
            rows = [r for r in rows if _matches_tag(r.get("tags"), tag)]
        rows = rows[:max_results]
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, "list_app_services", rows, params
        )

    async def _do_list_aks_clusters(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        rows_cached, key = await self._maybe_cached_rows(
            agent_context, client, sub_id, "aks", params,
            self._cache_filters(params),
        )
        if rows_cached is not None:
            return await self._tabular(
                agent_context, "list_aks_clusters",
                rows_cached[:max_results], params, cache_hit=True,
            )
        ac = client.aks(sub_id)
        rg = (params.get("resource_group") or "").strip()
        if rg:
            iterator = ac.managed_clusters.list_by_resource_group(rg)
        else:
            iterator = ac.managed_clusters.list()
        items = await client.collect(iterator, limit=_MAX_RESULTS)
        rows = [_slim_aks(c) for c in items]
        if tag := params.get("tag"):
            rows = [r for r in rows if _matches_tag(r.get("tags"), tag)]
        rows = rows[:max_results]
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, "list_aks_clusters", rows, params
        )

    async def _do_list_storage_accounts(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        rows_cached, key = await self._maybe_cached_rows(
            agent_context, client, sub_id, "storage_accounts", params,
            self._cache_filters(params),
        )
        if rows_cached is not None:
            return await self._tabular(
                agent_context, "list_storage_accounts",
                rows_cached[:max_results], params, cache_hit=True,
            )
        sc = client.storage(sub_id)
        rg = (params.get("resource_group") or "").strip()
        if rg:
            iterator = sc.storage_accounts.list_by_resource_group(rg)
        else:
            iterator = sc.storage_accounts.list()
        items = await client.collect(iterator, limit=_MAX_RESULTS)
        rows = [_slim_storage(a) for a in items]
        if tag := params.get("tag"):
            rows = [r for r in rows if _matches_tag(r.get("tags"), tag)]
        rows = rows[:max_results]
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, "list_storage_accounts", rows, params
        )

    async def _do_list_snapshots(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        sub_id = client.resolve_subscription(params)
        older_than_days = params.get("older_than_days")
        filters = {
            **self._cache_filters(params),
            "older_than_days": older_than_days,
        }
        rows_cached, key = await self._maybe_cached_rows(
            agent_context, client, sub_id, "snapshots", params, filters
        )
        if rows_cached is not None:
            return await self._tabular(
                agent_context, "list_snapshots",
                rows_cached[:max_results], params, cache_hit=True,
            )
        cc = client.compute(sub_id)
        rg = (params.get("resource_group") or "").strip()
        if rg:
            iterator = cc.snapshots.list_by_resource_group(rg)
        else:
            iterator = cc.snapshots.list()
        items = await client.collect(iterator, limit=_MAX_RESULTS)
        rows = [_slim_snapshot(s) for s in items]
        if older_than_days:
            cutoff = datetime.now(timezone.utc) - timedelta(
                days=int(older_than_days)
            )
            rows = [r for r in rows if _is_older_than(r.get("time_created"), cutoff)]
        if tag := params.get("tag"):
            rows = [r for r in rows if _matches_tag(r.get("tags"), tag)]
        rows = rows[:max_results]
        await cache_set(key, rows)
        return await self._tabular(
            agent_context, "list_snapshots", rows, params
        )

    async def _do_list_orphans(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        """Composite: orphan disks/IPs/NICs and snapshots > 90d."""
        sub_id = client.resolve_subscription(params)
        # Use the list_* helpers but force `attached=false` for IPs/disks.
        cc = client.compute(sub_id)
        nc = client.network(sub_id)
        rg = (params.get("resource_group") or "").strip()

        # Disks
        disks_iter = (
            cc.disks.list_by_resource_group(rg) if rg else cc.disks.list()
        )
        disks = [
            _slim_disk(d)
            for d in await client.collect(disks_iter, limit=_MAX_RESULTS)
        ]
        orphan_disks = [d for d in disks if not d.get("managed_by")]

        # Public IPs
        ips_iter = (
            nc.public_ip_addresses.list(rg) if rg
            else nc.public_ip_addresses.list_all()
        )
        ips = [
            _slim_public_ip(p)
            for p in await client.collect(ips_iter, limit=_MAX_RESULTS)
        ]
        orphan_ips = [p for p in ips if not p.get("associated_to")]

        # NICs
        nics_iter = (
            nc.network_interfaces.list(rg) if rg
            else nc.network_interfaces.list_all()
        )
        nics = [
            _slim_nic(n)
            for n in await client.collect(nics_iter, limit=_MAX_RESULTS)
        ]
        orphan_nics = [n for n in nics if not n.get("virtual_machine")]

        # Old snapshots (>90 days)
        snaps_iter = (
            cc.snapshots.list_by_resource_group(rg) if rg
            else cc.snapshots.list()
        )
        snaps = [
            _slim_snapshot(s)
            for s in await client.collect(snaps_iter, limit=_MAX_RESULTS)
        ]
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        old_snaps = [
            s for s in snaps
            if _is_older_than(s.get("time_created"), cutoff)
        ]

        groups = [
            {"kind": "disks", "count": len(orphan_disks),
             "items": orphan_disks[:max_results]},
            {"kind": "public_ips", "count": len(orphan_ips),
             "items": orphan_ips[:max_results]},
            {"kind": "nics", "count": len(orphan_nics),
             "items": orphan_nics[:max_results]},
            {"kind": "snapshots_older_than_90d",
             "count": len(old_snaps), "items": old_snaps[:max_results]},
        ]
        return {
            "subscription_id": sub_id,
            "resource_group": rg or None,
            "groups": groups,
            "totals": {g["kind"]: g["count"] for g in groups},
        }

    async def _do_describe_resource(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
        max_results: int,
    ) -> dict:
        rid = _require(params, "resource_id")
        sub_id = client.resolve_subscription(params)
        rc = client.resource(sub_id)
        # api-version omitted → SDK picks a recent default
        try:
            res = await client.call(
                rc.resources.get_by_id(rid, "2024-07-01")  # type: ignore[attr-defined]
            )
        except AzureError as exc:
            if exc.code == "NOT_FOUND":
                raise
            # Retry with a fallback api-version
            res = await client.call(
                rc.resources.get_by_id(rid, "2021-04-01")  # type: ignore[attr-defined]
            )
        return {"resource": _as_dict(res)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _power_state_from_iv(instance_view: Any) -> Optional[str]:
    iv = _as_dict(instance_view)
    statuses = iv.get("statuses") or []
    for s in statuses:
        code = s.get("code") or ""
        if code.startswith("PowerState/"):
            return code.split("/", 1)[1]
    return None


def _is_older_than(time_created: Any, cutoff: datetime) -> bool:
    if not time_created:
        return False
    try:
        if isinstance(time_created, str):
            ts = datetime.fromisoformat(
                time_created.replace("Z", "+00:00")
            )
        else:
            ts = time_created
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    return ts < cutoff


def _require(params: dict, field: str) -> str:
    val = params.get(field)
    if isinstance(val, str):
        val = val.strip()
    if not val:
        raise _ParamError(f"'{field}' is required for this action.")
    return str(val)


_ = Any
