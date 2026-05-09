"""gSage AI — Azure write actions on VMs (audited, approval-gated).

This tool handles the small set of repetitive operational tasks that
analysts need to perform on Azure compute resources:

- ``start_vm``                — Start a deallocated VM.
- ``stop_vm``                 — Stop a VM. ``mode='deallocate'`` (default,
                                stops billing) or ``mode='power_off'``
                                (still billed, retains memory state).
- ``restart_vm``              — Restart a VM.
- ``resize_vm``               — Change a VM's size. By default the new
                                SKU is prevalidated against the VM's
                                region via ``resource_skus.list``;
                                ``force=true`` bypasses the check.
- ``update_vm_tags``           — Merge tag updates into a VM (``replace=true``
                                replaces the entire tag set).
- ``add_shutdown_schedule``    — Configure auto-shutdown via the
                                ``Microsoft.DevTestLab/schedules`` resource
                                ``shutdown-computevm-{name}``.

Every call requires a ``reason`` string for audit logging and is
subject to the platform approval workflow (``requires_approval=True``).

Permission: ``azure:write``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar, Optional

from src.mcp_server.tools.base import BaseTool, ToolResult
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
    "start_vm",
    "stop_vm",
    "restart_vm",
    "resize_vm",
    "update_vm_tags",
    "add_shutdown_schedule",
})

_STOP_MODES = ("deallocate", "power_off")


class _ParamError(Exception):
    pass


class AzureManageTool(BaseTool):
    """Approval-gated write operations on Azure VMs.

    Every action requires a free-form ``reason`` for audit and is
    subject to the platform approval workflow.

    Permission: ``azure:write``.
    """

    name: ClassVar[str] = "azure_manage"
    version: ClassVar[str] = "1.0.0"
    summary: ClassVar[str] = (
        "Approval-gated Azure VM management: start/stop/restart/resize, "
        "update tags, configure auto-shutdown."
    )
    category: ClassVar[str] = "devops"
    config_namespace: ClassVar[str] = "azure"
    permissions: ClassVar[list[str]] = ["azure:write"]
    rate_limit_per_minute: ClassVar[int] = 20
    timeout_seconds: ClassVar[int] = 300
    use_circuit_breaker: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = True
    supports_multiple_configs: ClassVar[bool] = True
    requires_config: ClassVar[bool] = True

    audit_field_mapping: ClassVar[dict] = {
        "action": "action",
        "target_resource": "resource_group",
        "target_entities": "name",
    }

    params_schema: ClassVar[dict] = {
        "type": "object",
        "required": ["action", "resource_group", "name", "reason"],
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Which write operation to perform.",
            },
            "profile": {"type": "string"},
            "subscription_id": {"type": "string"},
            "resource_group": {
                "type": "string",
                "description": "Resource group of the target VM.",
            },
            "name": {
                "type": "string",
                "description": "VM name.",
            },
            "reason": {
                "type": "string",
                "minLength": 5,
                "description": (
                    "Free-form justification recorded in the audit log."
                ),
            },
            "mode": {
                "type": "string",
                "enum": list(_STOP_MODES),
                "description": (
                    "[stop_vm] 'deallocate' (default, stops billing) or "
                    "'power_off' (retains state, still billed)."
                ),
            },
            "vm_size": {
                "type": "string",
                "description": (
                    "[resize_vm] New Azure SKU (e.g. Standard_D4s_v5)."
                ),
            },
            "force": {
                "type": "boolean",
                "description": (
                    "[resize_vm] Bypass the per-region SKU availability "
                    "check (default false)."
                ),
            },
            "tags": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "[update_vm_tags] Tag dict to apply (merged by "
                    "default)."
                ),
            },
            "replace": {
                "type": "boolean",
                "description": (
                    "[update_vm_tags] Replace the entire tag set instead "
                    "of merging (default false)."
                ),
            },
            "shutdown_time": {
                "type": "string",
                "pattern": "^[0-2][0-9][0-5][0-9]$",
                "description": (
                    "[add_shutdown_schedule] Daily shutdown time in 24h "
                    "HHMM format (e.g. '1900')."
                ),
            },
            "time_zone": {
                "type": "string",
                "description": (
                    "[add_shutdown_schedule] IANA/Windows time zone "
                    "(e.g. 'E. South America Standard Time')."
                ),
            },
            "wait": {
                "type": "boolean",
                "description": (
                    "Wait for long-running operations to complete "
                    "(default true). Set to false to return immediately "
                    "with the LRO operation status."
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
            log.exception("azure_manage(%s): unexpected error", action)
            elapsed = int((time.monotonic() - t0) * 1000)
            return self._failure(
                "INTERNAL_ERROR", str(exc), execution_time_ms=elapsed
            )

        elapsed = int((time.monotonic() - t0) * 1000)
        return self._success(
            data={"action": action, **data}, execution_time_ms=elapsed
        )

    # ── Action handlers ─────────────────────────────────────────────────────

    async def _do_start_vm(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        rg, name = _require_target(params)
        sub_id = client.resolve_subscription(params)
        cc = client.compute(sub_id)
        wait = params.get("wait", True)
        log.info(
            "azure_manage start_vm sub=%s rg=%s vm=%s reason=%r",
            sub_id, rg, name, params.get("reason"),
        )
        poller = await client.call(
            cc.virtual_machines.begin_start(rg, name)
        )
        status = await _await_lro(poller, wait)
        return {
            "subscription_id": sub_id,
            "resource_group": rg,
            "name": name,
            "operation": "start",
            **status,
        }

    async def _do_stop_vm(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        rg, name = _require_target(params)
        sub_id = client.resolve_subscription(params)
        cc = client.compute(sub_id)
        mode = (params.get("mode") or "deallocate").strip()
        if mode not in _STOP_MODES:
            raise _ParamError(
                f"mode must be one of {list(_STOP_MODES)}, got {mode!r}."
            )
        wait = params.get("wait", True)
        log.info(
            "azure_manage stop_vm sub=%s rg=%s vm=%s mode=%s reason=%r",
            sub_id, rg, name, mode, params.get("reason"),
        )
        if mode == "deallocate":
            poller = await client.call(
                cc.virtual_machines.begin_deallocate(rg, name)
            )
        else:
            poller = await client.call(
                cc.virtual_machines.begin_power_off(rg, name)
            )
        status = await _await_lro(poller, wait)
        return {
            "subscription_id": sub_id,
            "resource_group": rg,
            "name": name,
            "operation": f"stop:{mode}",
            **status,
        }

    async def _do_restart_vm(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        rg, name = _require_target(params)
        sub_id = client.resolve_subscription(params)
        cc = client.compute(sub_id)
        wait = params.get("wait", True)
        log.info(
            "azure_manage restart_vm sub=%s rg=%s vm=%s reason=%r",
            sub_id, rg, name, params.get("reason"),
        )
        poller = await client.call(
            cc.virtual_machines.begin_restart(rg, name)
        )
        status = await _await_lro(poller, wait)
        return {
            "subscription_id": sub_id,
            "resource_group": rg,
            "name": name,
            "operation": "restart",
            **status,
        }

    async def _do_resize_vm(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        rg, name = _require_target(params)
        new_size = _require(params, "vm_size")
        sub_id = client.resolve_subscription(params)
        force = bool(params.get("force"))
        cc = client.compute(sub_id)

        # Fetch current VM to obtain location and tags.
        vm = await client.call(cc.virtual_machines.get(rg, name))
        vm_d = _as_dict(vm)
        location = vm_d.get("location") or ""
        current_size = (
            (vm_d.get("hardware_profile") or {}).get("vm_size")
        ) or ""

        # Prevalidate SKU availability per region (decision #2).
        if not force and location:
            try:
                allowed = await _list_skus_for_region(
                    client, sub_id, location
                )
                if new_size not in allowed:
                    raise AzureError(
                        "INVALID_PARAMS",
                        (
                            f"SKU '{new_size}' is not available in region "
                            f"'{location}'. Pass force=true to bypass this "
                            "check."
                        ),
                    )
            except AzureError:
                raise
            except Exception as exc:
                log.warning(
                    "resize_vm SKU prevalidation failed (continuing with "
                    "force=false attempt): %s", exc,
                )

        wait = params.get("wait", True)
        log.info(
            "azure_manage resize_vm sub=%s rg=%s vm=%s %s -> %s reason=%r",
            sub_id, rg, name, current_size, new_size, params.get("reason"),
        )

        # PATCH update via begin_update.
        update_body: Any = {
            "hardware_profile": {"vm_size": new_size},
        }
        poller = await client.call(
            cc.virtual_machines.begin_update(  # type: ignore[attr-defined,arg-type]
                rg, name, update_body
            )
        )
        status = await _await_lro(poller, wait)
        return {
            "subscription_id": sub_id,
            "resource_group": rg,
            "name": name,
            "operation": "resize",
            "from_size": current_size,
            "to_size": new_size,
            "force": force,
            **status,
        }

    async def _do_update_vm_tags(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        rg, name = _require_target(params)
        new_tags = params.get("tags") or {}
        if not isinstance(new_tags, dict) or not new_tags:
            raise _ParamError("'tags' must be a non-empty object.")
        replace = bool(params.get("replace"))
        sub_id = client.resolve_subscription(params)
        cc = client.compute(sub_id)
        wait = params.get("wait", True)

        if replace:
            final_tags = {str(k): str(v) for k, v in new_tags.items()}
        else:
            vm = await client.call(cc.virtual_machines.get(rg, name))
            current = _as_dict(vm).get("tags") or {}
            final_tags = {str(k): str(v) for k, v in current.items()}
            for k, v in new_tags.items():
                final_tags[str(k)] = str(v)

        log.info(
            "azure_manage update_vm_tags sub=%s rg=%s vm=%s replace=%s "
            "keys=%s reason=%r",
            sub_id, rg, name, replace, sorted(new_tags.keys()),
            params.get("reason"),
        )

        update_body: Any = {"tags": final_tags}
        poller = await client.call(
            cc.virtual_machines.begin_update(  # type: ignore[attr-defined,arg-type]
                rg, name, update_body
            )
        )
        status = await _await_lro(poller, wait)
        return {
            "subscription_id": sub_id,
            "resource_group": rg,
            "name": name,
            "operation": "update_tags",
            "replace": replace,
            "applied_keys": sorted(new_tags.keys()),
            "tag_count_after": len(final_tags),
            **status,
        }

    async def _do_add_shutdown_schedule(
        self,
        client: AzureClient,
        params: dict,
        agent_context: AgentContext,
    ) -> dict:
        rg, name = _require_target(params)
        time_str = _require(params, "shutdown_time")
        if (
            len(time_str) != 4
            or not time_str.isdigit()
            or int(time_str[:2]) > 23
            or int(time_str[2:]) > 59
        ):
            raise _ParamError(
                "shutdown_time must be 4 digits in HHMM format (00-23 / "
                "00-59)."
            )
        time_zone = (
            params.get("time_zone") or "E. South America Standard Time"
        ).strip()
        sub_id = client.resolve_subscription(params)
        cc = client.compute(sub_id)
        rc = client.resource(sub_id)

        # Fetch VM to obtain its full id and location.
        vm = await client.call(cc.virtual_machines.get(rg, name))
        vm_d = _as_dict(vm)
        target_id = vm_d.get("id") or ""
        location = vm_d.get("location") or ""

        schedule_name = f"shutdown-computevm-{name}"
        schedule_id = (
            f"/subscriptions/{sub_id}/resourceGroups/{rg}/providers/"
            f"Microsoft.DevTestLab/schedules/{schedule_name}"
        )
        body: Any = {
            "location": location,
            "properties": {
                "status": "Enabled",
                "taskType": "ComputeVmShutdownTask",
                "dailyRecurrence": {"time": time_str},
                "timeZoneId": time_zone,
                "notificationSettings": {"status": "Disabled"},
                "targetResourceId": target_id,
            },
        }
        log.info(
            "azure_manage add_shutdown_schedule sub=%s rg=%s vm=%s "
            "time=%s tz=%s reason=%r",
            sub_id, rg, name, time_str, time_zone, params.get("reason"),
        )

        wait = params.get("wait", True)
        poller = await client.call(
            rc.resources.begin_create_or_update_by_id(  # type: ignore[attr-defined,arg-type]
                schedule_id,
                "2018-09-15",
                body,
            )
        )
        status = await _await_lro(poller, wait)
        return {
            "subscription_id": sub_id,
            "resource_group": rg,
            "name": name,
            "operation": "add_shutdown_schedule",
            "schedule_id": schedule_id,
            "shutdown_time": time_str,
            "time_zone": time_zone,
            **status,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _await_lro(poller: Any, wait: bool) -> dict:
    """Wait on (or skip) an Azure LRO poller and return a status dict."""
    if not wait:
        return {
            "status": "in_progress",
            "wait": False,
        }
    try:
        result = await poller.result()
    except Exception as exc:
        from src.mcp_server.tools.devops.azure._client import _translate
        raise _translate(exc) from exc
    return {
        "status": "succeeded",
        "wait": True,
        "result": _as_dict(result) if result is not None else None,
    }


async def _list_skus_for_region(
    client: AzureClient, sub_id: str, region: str
) -> set[str]:
    """Return the set of VM SKU names available in ``region``."""
    cc = client.compute(sub_id)
    skus = await client.collect(
        cc.resource_skus.list(filter=f"location eq '{region}'"),
        limit=10000,
    )
    out: set[str] = set()
    region_lc = region.lower()
    for s in skus:
        d = _as_dict(s)
        if (d.get("resource_type") or "").lower() != "virtualmachines":
            continue
        sku_name = d.get("name") or ""
        locations = [
            (loc or "").lower() for loc in (d.get("locations") or [])
        ]
        if locations and region_lc not in locations:
            continue
        # Filter out SKUs marked as restricted in this region.
        restrictions = d.get("restrictions") or []
        restricted = False
        for r in restrictions:
            r_type = (r.get("type") or "").lower()
            r_locs = [
                (loc or "").lower()
                for loc in ((r.get("restriction_info") or {}).get(
                    "locations"
                ) or [])
            ]
            if r_type == "location" and (
                not r_locs or region_lc in r_locs
            ):
                restricted = True
                break
        if restricted:
            continue
        if sku_name:
            out.add(sku_name)
    return out


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


def _require_target(params: dict) -> tuple[str, str]:
    return _require(params, "resource_group"), _require(params, "name")


_ = Any
