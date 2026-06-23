"""gSage AI — Slim JSON views for pyVmomi managed objects.

pyVmomi managed objects are not JSON-serialisable and hold live SOAP
references. Every ``slim_*`` helper extracts only the fields the agent
needs into a flat ``dict`` so results can be cached, exported to CSV and
returned in a :class:`ToolResult` without dragging the SDK object along.

All helpers are defensive: vCenter omits large parts of an object's
property set depending on the entity's state (a powered-off VM has no
guest IP, a disconnected host has no ``summary.runtime`` data, …), so
every access is guarded and missing values become ``None``.
"""

from __future__ import annotations

from typing import Any, Optional


def _safe(fn: Any, default: Any = None) -> Any:
    """Call ``fn`` (a 0-arg lambda) returning ``default`` on any error/None."""
    try:
        val = fn()
        return val if val is not None else default
    except Exception:
        return default


def _moid(obj: Any) -> Optional[str]:
    """Stable managed-object id (e.g. 'vm-1024'), the vCenter primary key."""
    return _safe(lambda: str(obj._moId))


# ---------------------------------------------------------------------------
# Cluster / host
# ---------------------------------------------------------------------------


def slim_cluster(cluster: Any, *, detail: bool = False) -> dict:
    summ = _safe(lambda: cluster.summary)
    usage = _safe(lambda: summ.usageSummary)
    row = {
        "name": _safe(lambda: cluster.name),
        "moid": _moid(cluster),
        "datacenter": _parent_datacenter_name(cluster),
        "hosts_total": _safe(lambda: summ.numHosts),
        "hosts_effective": _safe(lambda: summ.numEffectiveHosts),
        "total_cpu_mhz": _safe(lambda: summ.totalCpu),
        "total_memory_bytes": _safe(lambda: summ.totalMemory),
        "num_vms": _safe(lambda: usage.totalVmCount),
        "drs_enabled": _safe(
            lambda: cluster.configuration.drsConfig.enabled
        ),
        "drs_behavior": _safe(
            lambda: str(cluster.configuration.drsConfig.defaultVmBehavior)
        ),
        "ha_enabled": _safe(
            lambda: cluster.configuration.dasConfig.enabled
        ),
        "overall_status": _safe(lambda: str(summ.overallStatus)),
    }
    if detail:
        row["hosts"] = [
            _safe(lambda h=h: h.name) for h in _safe(lambda: cluster.host, [])
        ]
        row["datastores"] = [
            _safe(lambda d=d: d.name)
            for d in _safe(lambda: cluster.datastore, [])
        ]
        row["networks"] = [
            _safe(lambda n=n: n.name)
            for n in _safe(lambda: cluster.network, [])
        ]
        row["resource_pools"] = [
            _safe(lambda p=p: p.name)
            for p in _safe(lambda: cluster.resourcePool.resourcePool, [])
        ]
    return row


def slim_host(host: Any, *, detail: bool = False) -> dict:
    summ = _safe(lambda: host.summary)
    hw = _safe(lambda: summ.hardware)
    qs = _safe(lambda: summ.quickStats)
    runtime = _safe(lambda: summ.runtime)
    row = {
        "name": _safe(lambda: host.name),
        "moid": _moid(host),
        "cluster": _safe(lambda: host.parent.name),
        "connection_state": _safe(lambda: str(runtime.connectionState)),
        "power_state": _safe(lambda: str(runtime.powerState)),
        "in_maintenance": _safe(lambda: runtime.inMaintenanceMode),
        "vendor": _safe(lambda: hw.vendor),
        "model": _safe(lambda: hw.model),
        "cpu_model": _safe(lambda: hw.cpuModel),
        "cpu_cores": _safe(lambda: hw.numCpuCores),
        "cpu_threads": _safe(lambda: hw.numCpuThreads),
        "cpu_mhz_per_core": _safe(lambda: hw.cpuMhz),
        "memory_bytes": _safe(lambda: hw.memorySize),
        "cpu_usage_mhz": _safe(lambda: qs.overallCpuUsage),
        "memory_usage_mb": _safe(lambda: qs.overallMemoryUsage),
        "uptime_seconds": _safe(lambda: qs.uptime),
        "esxi_version": _safe(lambda: summ.config.product.fullName),
        "overall_status": _safe(lambda: str(summ.overallStatus)),
    }
    if detail:
        row["num_vms"] = len(_safe(lambda: host.vm, []))
        row["datastores"] = [
            _safe(lambda d=d: d.name) for d in _safe(lambda: host.datastore, [])
        ]
        row["networks"] = [
            _safe(lambda n=n: n.name) for n in _safe(lambda: host.network, [])
        ]
    return row


# ---------------------------------------------------------------------------
# Virtual machine
# ---------------------------------------------------------------------------


def slim_vm(vm: Any, *, detail: bool = False) -> dict:
    summ = _safe(lambda: vm.summary)
    cfg = _safe(lambda: summ.config)
    runtime = _safe(lambda: summ.runtime)
    guest = _safe(lambda: summ.guest)
    qs = _safe(lambda: summ.quickStats)
    row = {
        "name": _safe(lambda: vm.name),
        "moid": _moid(vm),
        "is_template": _safe(lambda: cfg.template, False),
        "power_state": _safe(lambda: str(runtime.powerState)),
        "guest_os": _safe(lambda: cfg.guestFullName),
        "num_cpu": _safe(lambda: cfg.numCpu),
        "memory_mb": _safe(lambda: cfg.memorySizeMB),
        "host": _safe(lambda: runtime.host.name),
        "guest_hostname": _safe(lambda: guest.hostName),
        "guest_ip": _safe(lambda: guest.ipAddress),
        "tools_status": _safe(lambda: str(guest.toolsStatus)),
        "tools_running": _safe(lambda: str(guest.toolsRunningStatus)),
        "uuid": _safe(lambda: cfg.instanceUuid),
        "annotation": _safe(lambda: cfg.annotation),
        "cpu_usage_mhz": _safe(lambda: qs.overallCpuUsage),
        "memory_usage_mb": _safe(lambda: qs.guestMemoryUsage),
        "storage_committed_bytes": _safe(lambda: summ.storage.committed),
        "overall_status": _safe(lambda: str(summ.overallStatus)),
    }
    if detail:
        row["disks"] = _vm_disks(vm)
        row["nics"] = _vm_nics(vm)
        row["datastores"] = [
            _safe(lambda d=d: d.name) for d in _safe(lambda: vm.datastore, [])
        ]
        row["networks"] = [
            _safe(lambda n=n: n.name) for n in _safe(lambda: vm.network, [])
        ]
        row["resource_pool"] = _safe(lambda: vm.resourcePool.name)
        row["all_guest_ips"] = _vm_guest_ips(vm)
        row["folder"] = _safe(lambda: vm.parent.name)
    return row


def _vm_disks(vm: Any) -> list[dict]:
    out: list[dict] = []
    devices = _safe(lambda: vm.config.hardware.device, [])
    for dev in devices:
        # Identify a VirtualDisk by attribute presence (capacityInKB) rather
        # than importing pyVmomi types into this pure-view module.
        if not hasattr(dev, "capacityInKB"):
            continue
        backing = _safe(lambda d=dev: d.backing)
        out.append({
            "label": _safe(lambda d=dev: d.deviceInfo.label),
            "key": _safe(lambda d=dev: d.key),
            "capacity_gb": _safe(
                lambda d=dev: round((d.capacityInKB or 0) / 1024 / 1024, 2)
            ),
            "datastore": _safe(lambda b=backing: b.datastore.name),
            "file_name": _safe(lambda b=backing: b.fileName),
            "thin_provisioned": _safe(lambda b=backing: b.thinProvisioned),
        })
    return out


def _vm_nics(vm: Any) -> list[dict]:
    out: list[dict] = []
    devices = _safe(lambda: vm.config.hardware.device, [])
    for dev in devices:
        # Virtual NICs expose a macAddress attribute.
        if not hasattr(dev, "macAddress"):
            continue
        backing = _safe(lambda d=dev: d.backing)
        out.append({
            "label": _safe(lambda d=dev: d.deviceInfo.label),
            "type": type(dev).__name__,
            "mac_address": _safe(lambda d=dev: d.macAddress),
            "network": (
                _safe(lambda b=backing: b.deviceName)
                or _safe(lambda b=backing: b.network.name)
                or _safe(lambda b=backing: b.port.portgroupKey)
            ),
            "connected": _safe(lambda d=dev: d.connectable.connected),
        })
    return out


def _vm_guest_ips(vm: Any) -> list[str]:
    ips: list[str] = []
    nets = _safe(lambda: vm.guest.net, [])
    for n in nets:
        for ip in _safe(lambda n=n: n.ipAddress, []) or []:
            if ip:
                ips.append(str(ip))
    return ips


# ---------------------------------------------------------------------------
# Datastore / network / resource pool / folder / datacenter
# ---------------------------------------------------------------------------


def slim_datastore(ds: Any) -> dict:
    summ = _safe(lambda: ds.summary)
    return {
        "name": _safe(lambda: ds.name),
        "moid": _moid(ds),
        "type": _safe(lambda: summ.type),
        "capacity_bytes": _safe(lambda: summ.capacity),
        "free_bytes": _safe(lambda: summ.freeSpace),
        "uncommitted_bytes": _safe(lambda: summ.uncommitted),
        "accessible": _safe(lambda: summ.accessible),
        "num_vms": len(_safe(lambda: ds.vm, [])),
    }


def slim_network(net: Any) -> dict:
    return {
        "name": _safe(lambda: net.name),
        "moid": _moid(net),
        "type": type(net).__name__,
        "accessible": _safe(lambda: net.summary.accessible),
        "num_vms": len(_safe(lambda: net.vm, [])),
    }


def slim_resource_pool(rp: Any) -> dict:
    cfg = _safe(lambda: rp.config)
    return {
        "name": _safe(lambda: rp.name),
        "moid": _moid(rp),
        "parent": _safe(lambda: rp.parent.name),
        "cpu_shares": _safe(lambda: cfg.cpuAllocation.shares.shares),
        "cpu_limit_mhz": _safe(lambda: cfg.cpuAllocation.limit),
        "memory_limit_mb": _safe(lambda: cfg.memoryAllocation.limit),
        "num_vms": len(_safe(lambda: rp.vm, [])),
    }


def slim_folder(folder: Any) -> dict:
    return {
        "name": _safe(lambda: folder.name),
        "moid": _moid(folder),
        "parent": _safe(lambda: folder.parent.name),
        "child_type": [str(t) for t in _safe(lambda: folder.childType, [])],
        "num_children": len(_safe(lambda: folder.childEntity, [])),
    }


def slim_datacenter(dc: Any) -> dict:
    return {
        "name": _safe(lambda: dc.name),
        "moid": _moid(dc),
        "vm_folder": _safe(lambda: dc.vmFolder.name),
        "host_folder": _safe(lambda: dc.hostFolder.name),
    }


# ---------------------------------------------------------------------------
# Snapshots / tasks / events
# ---------------------------------------------------------------------------


def slim_snapshot_tree(vm: Any) -> list[dict]:
    """Flatten a VM's snapshot tree into a list of dicts (depth recorded)."""
    snap_info = _safe(lambda: vm.snapshot)
    if snap_info is None:
        return []
    current = _safe(lambda: snap_info.currentSnapshot)
    current_moid = _safe(lambda: str(current._moId)) if current else None

    out: list[dict] = []

    def _walk(nodes: Any, depth: int) -> None:
        for node in nodes or []:
            moid = _safe(lambda n=node: str(n.snapshot._moId))
            out.append({
                "name": _safe(lambda n=node: n.name),
                "description": _safe(lambda n=node: n.description),
                "moid": moid,
                "created": _safe(lambda n=node: n.createTime.isoformat()),
                "state": _safe(lambda n=node: str(n.state)),
                "depth": depth,
                "is_current": moid == current_moid,
            })
            _walk(_safe(lambda n=node: n.childSnapshotList, []), depth + 1)

    _walk(_safe(lambda: snap_info.rootSnapshotList, []), 0)
    return out


def slim_task(task_info: Any) -> dict:
    return {
        "key": _safe(lambda: task_info.key),
        "description": _safe(lambda: task_info.descriptionId),
        "entity": _safe(lambda: task_info.entityName),
        "state": _safe(lambda: str(task_info.state)),
        "queued": _safe(lambda: task_info.queueTime.isoformat()),
        "started": _safe(lambda: task_info.startTime.isoformat()),
        "completed": _safe(lambda: task_info.completeTime.isoformat()),
        "user": _safe(lambda: task_info.reason.userName),
        "error": _safe(lambda: task_info.error.msg),
    }


def slim_event(event: Any) -> dict:
    return {
        "key": _safe(lambda: event.key),
        "type": type(event).__name__,
        "created": _safe(lambda: event.createdTime.isoformat()),
        "user": _safe(lambda: event.userName),
        "datacenter": _safe(lambda: event.datacenter.name),
        "host": _safe(lambda: event.host.name),
        "vm": _safe(lambda: event.vm.name),
        "message": _safe(lambda: event.fullFormattedMessage),
    }


def _parent_datacenter_name(obj: Any) -> Optional[str]:
    """Walk parents up to the enclosing Datacenter and return its name."""
    cur = _safe(lambda: obj.parent)
    for _ in range(20):  # bounded walk
        if cur is None:
            return None
        # A Datacenter is the only inventory object exposing ``vmFolder``.
        if hasattr(cur, "vmFolder"):
            return _safe(lambda c=cur: c.name)
        cur = _safe(lambda c=cur: c.parent)
    return None


__all__ = [
    "slim_cluster",
    "slim_datacenter",
    "slim_datastore",
    "slim_event",
    "slim_folder",
    "slim_host",
    "slim_network",
    "slim_resource_pool",
    "slim_snapshot_tree",
    "slim_task",
    "slim_vm",
]
