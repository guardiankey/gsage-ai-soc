"""gSage AI — Slim JSON views for Proxmox VE API responses.

The Proxmox API already returns plain JSON dicts, so unlike the vCenter
``_views`` (which unwraps live SOAP objects) these helpers simply select,
rename and normalise the handful of fields the agent needs — turning raw
byte counts into GB, integer flags into booleans, and keeping result rows
small and stable for caching / CSV export.
"""

from __future__ import annotations

from typing import Any, Optional


def _gb(num_bytes: Any) -> Optional[float]:
    try:
        return round(float(num_bytes) / (1024 ** 3), 2)
    except (TypeError, ValueError):
        return None


def _bool(flag: Any) -> bool:
    return str(flag) in ("1", "True", "true")


def slim_guest_resource(r: dict[str, Any]) -> dict:
    """Slim a ``/cluster/resources?type=vm`` row (QEMU or LXC)."""
    maxcpu = r.get("maxcpu")
    cpu_frac = r.get("cpu")
    return {
        "vmid": r.get("vmid"),
        "name": r.get("name"),
        "kind": r.get("type"),  # 'qemu' | 'lxc'
        "node": r.get("node"),
        "status": r.get("status"),  # running | stopped
        "is_template": _bool(r.get("template")),
        "max_cpu": maxcpu,
        "cpu_percent": (
            round(float(cpu_frac) * 100, 1) if cpu_frac is not None else None
        ),
        "max_memory_gb": _gb(r.get("maxmem")),
        "memory_gb": _gb(r.get("mem")),
        "max_disk_gb": _gb(r.get("maxdisk")),
        "disk_gb": _gb(r.get("disk")),
        "uptime_seconds": r.get("uptime"),
        "pool": r.get("pool"),
        "tags": r.get("tags"),
    }


def slim_node(r: dict[str, Any]) -> dict:
    """Slim a ``/nodes`` row."""
    maxcpu = r.get("maxcpu")
    cpu_frac = r.get("cpu")
    return {
        "node": r.get("node"),
        "status": r.get("status"),  # online | offline
        "cpu_percent": (
            round(float(cpu_frac) * 100, 1) if cpu_frac is not None else None
        ),
        "max_cpu": maxcpu,
        "memory_gb": _gb(r.get("mem")),
        "max_memory_gb": _gb(r.get("maxmem")),
        "disk_gb": _gb(r.get("disk")),
        "max_disk_gb": _gb(r.get("maxdisk")),
        "uptime_seconds": r.get("uptime"),
        "level": r.get("level"),
        "ssl_fingerprint": r.get("ssl_fingerprint"),
    }


def slim_node_status(r: dict[str, Any]) -> dict:
    """Slim a ``/nodes/{node}/status`` detail object."""
    cpuinfo = r.get("cpuinfo") or {}
    memory = r.get("memory") or {}
    rootfs = r.get("rootfs") or {}
    loadavg = r.get("loadavg") or []
    return {
        "uptime_seconds": r.get("uptime"),
        "pve_version": (r.get("pveversion")),
        "kernel": r.get("kversion") or r.get("current-kernel"),
        "cpu_model": cpuinfo.get("model"),
        "cpu_sockets": cpuinfo.get("sockets"),
        "cpu_cores": cpuinfo.get("cores"),
        "cpu_total_threads": cpuinfo.get("cpus"),
        "load_avg": loadavg,
        "memory_total_gb": _gb(memory.get("total")),
        "memory_used_gb": _gb(memory.get("used")),
        "memory_free_gb": _gb(memory.get("free")),
        "rootfs_total_gb": _gb(rootfs.get("total")),
        "rootfs_used_gb": _gb(rootfs.get("used")),
    }


def slim_storage(r: dict[str, Any]) -> dict:
    """Slim a ``/nodes/{node}/storage`` row."""
    used_raw = r.get("used")
    total_raw = r.get("total")
    return {
        "storage": r.get("storage"),
        "type": r.get("type"),
        "content": r.get("content"),
        "enabled": _bool(r.get("enabled")) if r.get("enabled") is not None else None,
        "active": _bool(r.get("active")) if r.get("active") is not None else None,
        "shared": _bool(r.get("shared")) if r.get("shared") is not None else None,
        "total_gb": _gb(total_raw),
        "used_gb": _gb(used_raw),
        "available_gb": _gb(r.get("avail")),
        "used_percent": (
            round(float(used_raw) / float(total_raw) * 100, 1)
            if used_raw is not None and total_raw is not None
            else None
        ),
    }


def slim_network(r: dict[str, Any]) -> dict:
    """Slim a ``/nodes/{node}/network`` interface row."""
    return {
        "iface": r.get("iface"),
        "type": r.get("type"),
        "active": _bool(r.get("active")) if r.get("active") is not None else None,
        "autostart": _bool(r.get("autostart")) if r.get("autostart") is not None else None,
        "method": r.get("method"),
        "address": r.get("address"),
        "netmask": r.get("netmask"),
        "gateway": r.get("gateway"),
        "bridge_ports": r.get("bridge_ports"),
        "cidr": r.get("cidr"),
    }


def slim_snapshot(r: dict[str, Any]) -> dict:
    """Slim a ``/.../snapshot`` row."""
    return {
        "name": r.get("name"),
        "description": r.get("description"),
        "parent": r.get("parent"),
        "snaptime": r.get("snaptime"),
        "has_vmstate": _bool(r.get("vmstate")) if r.get("vmstate") is not None else None,
        "is_current": r.get("name") == "current",
    }


def slim_task(r: dict[str, Any]) -> dict:
    """Slim a ``/cluster/tasks`` or ``/nodes/{node}/tasks`` row."""
    return {
        "upid": r.get("upid"),
        "type": r.get("type"),
        "node": r.get("node"),
        "vmid": r.get("id") or r.get("vmid"),
        "user": r.get("user"),
        "status": r.get("status"),
        "starttime": r.get("starttime"),
        "endtime": r.get("endtime"),
    }


def slim_guest_config(kind: str, vmid: int, node: str, config: dict[str, Any], status: dict[str, Any]) -> dict:
    """Merge a guest's ``config`` + ``status/current`` into one flat view.

    Disk and network entries (scsi0, virtio0, net0, rootfs, …) are passed
    through as-is under ``disks`` / ``nics`` since their option strings are
    Proxmox-specific and most useful verbatim to an operator.
    """
    disks: dict = {}
    nics: dict = {}
    other: dict = {}
    cloudinit: dict = {}
    disk_prefixes = ("scsi", "virtio", "ide", "sata", "mp", "efidisk", "tpmstate")
    for key, val in (config or {}).items():
        if key.startswith("net") and key[3:].isdigit():
            nics[key] = val
        elif key.startswith("ipconfig") or key in ("ciuser", "cipassword", "sshkeys", "nameserver", "searchdomain"):
            cloudinit[key] = val
        elif key == "rootfs" or (
            any(key.startswith(p) for p in disk_prefixes) and key[-1:].isdigit()
        ):
            disks[key] = val
        elif key in ("name", "hostname", "cores", "sockets", "memory", "ostype", "boot", "agent", "onboot", "description", "tags", "arch"):
            other[key] = val
        else:
            other.setdefault("_extra", {})[key] = val

    cpu_raw = status.get("cpu")
    return {
        "vmid": vmid,
        "kind": kind,
        "node": node,
        "name": config.get("name") or config.get("hostname"),
        "status": status.get("status"),
        "is_template": _bool(config.get("template")),
        "cores": config.get("cores"),
        "sockets": config.get("sockets"),
        "memory_mb": config.get("memory"),
        "ostype": config.get("ostype"),
        "agent": config.get("agent"),
        "onboot": _bool(config.get("onboot")) if config.get("onboot") is not None else None,
        "uptime_seconds": status.get("uptime"),
        "cpu_percent": (
            round(float(cpu_raw) * 100, 1)
            if cpu_raw is not None else None
        ),
        "memory_used_gb": _gb(status.get("mem")),
        "max_memory_gb": _gb(status.get("maxmem")),
        "disks": disks,
        "nics": nics,
        "cloudinit": cloudinit or None,
        "config": other,
    }


def slim_guest_metrics(kind: str, vmid: int, status: dict[str, Any]) -> dict:
    """Slim a ``/.../status/current`` row into a metrics view."""
    cpu_raw = status.get("cpu")
    return {
        "vmid": vmid,
        "kind": kind,
        "status": status.get("status"),
        "cpu_percent": (
            round(float(cpu_raw) * 100, 1)
            if cpu_raw is not None else None
        ),
        "cpus": status.get("cpus"),
        "memory_used_gb": _gb(status.get("mem")),
        "max_memory_gb": _gb(status.get("maxmem")),
        "disk_read_bytes": status.get("diskread"),
        "disk_write_bytes": status.get("diskwrite"),
        "net_in_bytes": status.get("netin"),
        "net_out_bytes": status.get("netout"),
        "uptime_seconds": status.get("uptime"),
        "ha_managed": (status.get("ha") or {}).get("managed"),
    }


__all__ = [
    "slim_guest_config",
    "slim_guest_metrics",
    "slim_guest_resource",
    "slim_network",
    "slim_node",
    "slim_node_status",
    "slim_snapshot",
    "slim_storage",
    "slim_task",
]
