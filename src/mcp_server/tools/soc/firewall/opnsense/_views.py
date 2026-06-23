"""gSage AI — Slim JSON views for OPNsense API responses.

OPNsense returns plain JSON, but field naming varies across endpoints and
versions (e.g. a firewall-log source is ``src`` while a state-table source
is ``src_addr`` or ``source``). Each ``slim_*`` helper selects the handful
of fields an analyst needs into a flat, stable dict — tolerating missing or
alternately-named keys via :func:`_g`.
"""

from __future__ import annotations

from typing import Any, Optional


def _g(row: dict, *keys: str, default: Any = None) -> Any:
    """Return the first present, non-empty value among ``keys``."""
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return default


def _bool(flag: Any) -> Optional[bool]:
    if flag in (None, ""):
        return None
    return str(flag) in ("1", "True", "true", "yes", "on")


def slim_alias(row: dict) -> dict:
    """Slim a ``/firewall/alias/searchItem`` row."""
    content = _g(row, "content", "current_items", default="")
    if isinstance(content, str):
        entries = [c for c in content.replace(",", "\n").split("\n") if c.strip()]
    elif isinstance(content, list):
        entries = content
    else:
        entries = []
    return {
        "uuid": row.get("uuid"),
        "name": _g(row, "name"),
        "type": _g(row, "type"),
        "enabled": _bool(row.get("enabled")),
        "description": _g(row, "description"),
        "entry_count": len(entries),
        "entries_preview": entries[:20],
    }


def slim_rule(row: dict) -> dict:
    """Slim a ``/firewall/filter/searchRule`` row."""
    return {
        "uuid": row.get("uuid"),
        "enabled": _bool(row.get("enabled")),
        "sequence": _g(row, "sequence"),
        "action": _g(row, "action"),
        "quick": _bool(row.get("quick")),
        "interface": _g(row, "interface"),
        "direction": _g(row, "direction"),
        "ip_protocol": _g(row, "ipprotocol"),
        "protocol": _g(row, "protocol"),
        "source": _g(row, "source_net", "source"),
        "source_port": _g(row, "source_port"),
        "destination": _g(row, "destination_net", "destination"),
        "destination_port": _g(row, "destination_port"),
        "log": _bool(row.get("log")),
        "description": _g(row, "description"),
    }


def slim_log_entry(row: dict) -> dict:
    """Slim a ``/diagnostics/firewall/log`` entry."""
    return {
        "time": _g(row, "__timestamp__", "time", "timestamp"),
        "action": _g(row, "action"),
        "interface": _g(row, "interface", "ifname"),
        "direction": _g(row, "dir", "direction"),
        "protocol": _g(row, "protoname", "protocol", "proto"),
        "src": _g(row, "src"),
        "src_port": _g(row, "srcport", "src_port"),
        "dst": _g(row, "dst"),
        "dst_port": _g(row, "dstport", "dst_port"),
        "label": _g(row, "label"),
        "rule_id": _g(row, "rid", "rulenr"),
    }


def slim_state(row: dict) -> dict:
    """Slim a ``/diagnostics/firewall/queryStates`` row."""
    return {
        "protocol": _g(row, "proto", "ipproto", "protocol"),
        "source": _g(row, "src_addr", "source", "src"),
        "source_port": _g(row, "src_port"),
        "destination": _g(row, "dst_addr", "destination", "dst"),
        "destination_port": _g(row, "dst_port"),
        "nat_address": _g(row, "nat_addr"),
        "direction": _g(row, "direction", "dir"),
        "state": _g(row, "state"),
        "interface": _g(row, "iface", "interface"),
        "description": _g(row, "descr", "label"),
    }


def slim_ids_alert(row: dict) -> dict:
    """Slim a ``/ids/service/queryAlerts`` row (Suricata)."""
    return {
        "timestamp": _g(row, "timestamp", "time"),
        "signature": _g(row, "alert", "signature", "msg"),
        "sid": _g(row, "sid"),
        "category": _g(row, "class", "category"),
        "severity": _g(row, "severity", "alert_severity", "priority"),
        "action": _g(row, "action", "alert_action"),
        "interface": _g(row, "interface"),
        "src_ip": _g(row, "src_ip", "source_ip"),
        "src_port": _g(row, "src_port"),
        "dest_ip": _g(row, "dest_ip", "destination_ip"),
        "dest_port": _g(row, "dest_port"),
        "protocol": _g(row, "proto", "protocol"),
    }


def slim_gateway(row: dict) -> dict:
    """Slim a ``/routes/gateway/status`` item."""
    return {
        "name": _g(row, "name"),
        "address": _g(row, "address"),
        "status": _g(row, "status", "status_translated"),
        "loss": _g(row, "loss"),
        "delay": _g(row, "delay"),
        "stddev": _g(row, "stddev"),
    }


def slim_lease(row: dict) -> dict:
    """Slim a ``/dhcpv4/leases/searchLease`` row."""
    return {
        "address": _g(row, "address", "ip"),
        "mac": _g(row, "mac"),
        "hostname": _g(row, "hostname"),
        "state": _g(row, "state", "status"),
        "interface": _g(row, "if", "interface"),
        "starts": _g(row, "starts"),
        "ends": _g(row, "ends"),
    }


__all__ = [
    "slim_alias",
    "slim_gateway",
    "slim_ids_alert",
    "slim_lease",
    "slim_log_entry",
    "slim_rule",
    "slim_state",
]
