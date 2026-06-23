"""gSage AI — Slim JSON views for SentinelOne API responses.

SentinelOne returns rich, deeply-nested objects. Each ``slim_*`` helper
selects the fields a SOC analyst needs into a flat, stable dict so results
stay small for caching / CSV export and don't flood the agent context.
"""

from __future__ import annotations

from typing import Any, Optional


def _g(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def slim_agent(a: dict) -> dict:
    """Slim a ``/agents`` row (an endpoint/agent)."""
    net_ifaces = a.get("networkInterfaces") or []
    ips: list[str] = []
    for nic in net_ifaces:
        for ip in (nic.get("inet") or []):
            if ip:
                ips.append(str(ip))
    return {
        "id": a.get("id"),
        "computer_name": _g(a, "computerName"),
        "uuid": _g(a, "uuid"),
        "os_name": _g(a, "osName"),
        "os_type": _g(a, "osType"),
        "agent_version": _g(a, "agentVersion"),
        "domain": _g(a, "domain"),
        "last_active": _g(a, "lastActiveDate"),
        "is_active": a.get("isActive"),
        "is_up_to_date": a.get("isUpToDate"),
        "infected": a.get("infected"),
        "active_threats": a.get("activeThreats"),
        "network_status": _g(a, "networkStatus"),  # connected/disconnected
        "is_isolated": (a.get("networkStatus") == "disconnected"),
        "machine_type": _g(a, "machineType"),
        "site_name": _g(a, "siteName"),
        "group_name": _g(a, "groupName"),
        "last_ip": _g(a, "lastIpToMgmt", "externalIp"),
        "interface_ips": ips[:5],
        "scan_status": _g(a, "scanStatus"),
    }


def slim_threat(t: dict) -> dict:
    """Slim a ``/threats`` row (flattening the v2.1 nested sub-objects)."""
    info = t.get("threatInfo") or {}
    agent = t.get("agentRealtimeInfo") or {}
    mitig = t.get("mitigationStatus") or []
    mitig_status = ", ".join(
        m.get("action") for m in mitig if isinstance(m, dict) and m.get("action")
    ) if isinstance(mitig, list) else None
    return {
        "id": t.get("id"),
        "threat_name": _g(info, "threatName"),
        "classification": _g(info, "classification"),
        "confidence_level": _g(info, "confidenceLevel"),
        "analyst_verdict": _g(info, "analystVerdict"),
        "incident_status": _g(info, "incidentStatus"),
        "mitigation_status": _g(info, "mitigationStatus") or mitig_status,
        "sha1": _g(info, "sha1"),
        "file_path": _g(info, "filePath"),
        "process_user": _g(info, "processUser"),
        "detection_type": _g(info, "detectionType"),
        "engines": info.get("engines"),
        "created_at": _g(info, "createdAt", "identifiedAt"),
        "agent_id": _g(agent, "agentId"),
        "computer_name": _g(agent, "agentComputerName"),
        "agent_os": _g(agent, "agentOsType"),
        "site_name": _g(agent, "siteName"),
    }


def slim_group(g: dict) -> dict:
    return {
        "id": g.get("id"),
        "name": _g(g, "name"),
        "type": _g(g, "type"),
        "site_id": _g(g, "siteId"),
        "total_agents": g.get("totalAgents"),
        "is_default": g.get("isDefault"),
    }


def slim_site(s: dict) -> dict:
    return {
        "id": s.get("id"),
        "name": _g(s, "name"),
        "state": _g(s, "state"),
        "account_name": _g(s, "accountName"),
        "total_licenses": s.get("totalLicenses"),
        "active_licenses": s.get("activeLicenses"),
        "expiration": _g(s, "expiration"),
    }


def slim_activity(a: dict) -> dict:
    return {
        "id": a.get("id"),
        "created_at": _g(a, "createdAt"),
        "activity_type": _g(a, "activityType"),
        "primary_description": _g(a, "primaryDescription"),
        "agent_id": _g(a, "agentId"),
        "user_id": _g(a, "userId"),
        "site_id": _g(a, "siteId"),
    }


def slim_blocklist_item(b: dict) -> dict:
    """Slim a ``/restrictions`` (blocklist) row."""
    return {
        "id": b.get("id"),
        "type": _g(b, "type"),
        "value": _g(b, "value"),
        "os_type": _g(b, "osType"),
        "description": _g(b, "description"),
        "scope": _g(b, "scope", "scopeName"),
        "source": _g(b, "source"),
        "created_at": _g(b, "createdAt"),
    }


def slim_note(n: dict) -> dict:
    return {
        "id": n.get("id"),
        "text": _g(n, "text"),
        "creator": _g(n, "creator"),
        "created_at": _g(n, "createdAt"),
    }


def first_or_none(body: dict) -> Optional[dict]:
    """Return the first ``data`` row from a list response, or the dict data."""
    data = body.get("data")
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        return data
    return None


__all__ = [
    "first_or_none",
    "slim_activity",
    "slim_agent",
    "slim_blocklist_item",
    "slim_group",
    "slim_note",
    "slim_site",
    "slim_threat",
]
