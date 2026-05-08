"""gSage AI — GravityZone result-shaping helpers.

GravityZone-specific normalisers and enrichers that prepare raw API rows
for the shared :mod:`src.mcp_server.tools.result_export` pipeline.

Responsibilities:

- Map raw RPC field names onto stable, snake_case column names that look
  good in CSV and on the agent's prompt.
- Reverse-code internal integer enums into human-readable strings (PHASR
  category / action_taken / type, machine type).
- Resolve foreign-key-style fields (``groupId`` → group name) using a
  best-effort, per-call cache so the agent / user sees meaningful labels
  without an extra round-trip per row.
- Provide tool-tuned ``DEFAULT_GROUP_KEYS`` lists for the top-N
  summariser.

All enrichers degrade gracefully: when the upstream metadata cannot be
fetched (insufficient API key permissions, RPC error) the original raw
value is preserved and a debug log is emitted.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.mcp_server.tools.soc.edr.gravityzone._client import (
    GravityZoneClient,
    GravityZoneError,
)

log = logging.getLogger(__name__)


# ── Reverse maps for PHASR int enums ────────────────────────────────────────

PHASR_CATEGORY_LABELS: dict[int, str] = {
    1: "tampering_tool",
    2: "hack_tool",
    3: "remote_tool",
    4: "miner",
    5: "lol_bin",
}
PHASR_ACTION_TAKEN_LABELS: dict[int, str] = {
    0: "action_needed",
    1: "applied",
    2: "partially_applied",
}
PHASR_TYPE_LABELS: dict[int, str] = {
    0: "allow_access",
    1: "restrict_access",
    2: "allow_access_request",
}

MACHINE_TYPE_LABELS: dict[int, str] = {
    0: "other",
    1: "computer",
    2: "virtual_machine",
    3: "ec2_instance",
}


# ── Endpoint normalisation + enrichment ─────────────────────────────────────

ENDPOINT_DEFAULT_GROUP_KEYS: tuple[str, ...] = (
    "is_managed",
    "machine_type",
    "os_version",
    "group_name",
    "policy_name",
    "managed_with_best",
    "product_outdated",
    "ssid",
)


def normalize_endpoint(raw: dict, *, group_name_by_id: Optional[dict[str, str]] = None) -> dict:
    """Flatten a GravityZone endpoint record for tabular display.

    ``group_name_by_id`` is an optional cache populated by
    :func:`build_group_name_cache`. When provided, ``group_name`` is
    derived from ``raw['groupId']``.
    """
    machine_type_int = raw.get("machineType", 0)
    policy = raw.get("policy") or {}
    if not isinstance(policy, dict):
        policy = {}
    macs = raw.get("macs", []) or []
    out: dict[str, Any] = {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "label": raw.get("label"),
        "fqdn": raw.get("fqdn"),
        "ip": raw.get("ip"),
        "macs": ",".join(macs) if isinstance(macs, list) else macs,
        "group_id": raw.get("groupId"),
        "is_managed": raw.get("isManaged"),
        "machine_type": MACHINE_TYPE_LABELS.get(int(machine_type_int or 0), "other"),
        "os_version": raw.get("operatingSystemVersion"),
        "managed_with_best": raw.get("managedWithBest"),
        "is_container_host": raw.get("isContainerHost"),
        "managed_relay": raw.get("managedRelay"),
        "security_server": raw.get("securityServer"),
        "product_outdated": raw.get("productOutdated"),
        "policy_id": policy.get("id"),
        "policy_name": policy.get("name"),
        "policy_applied": policy.get("applied"),
        "last_successful_scan": raw.get("lastSuccessfulScan"),
        "ssid": raw.get("ssid"),
    }
    gid = out.get("group_id")
    if group_name_by_id and isinstance(gid, str):
        out["group_name"] = group_name_by_id.get(gid)
    else:
        out["group_name"] = None
    return out


async def build_group_name_cache(
    client: GravityZoneClient,
    *,
    parent_id: Optional[str] = None,
) -> dict[str, str]:
    """Best-effort ``groupId → group name`` lookup.

    Walks ``network.getCustomGroupsList`` recursively, swallowing any
    GravityZone error so endpoint enrichment never fails because of an
    inventory permission gap.
    """
    cache: dict[str, str] = {}

    async def _walk(pid: Optional[str]) -> None:
        try:
            params: dict[str, Any] = {}
            if pid:
                params["parentId"] = pid
            result = await client.call("network", "getCustomGroupsList", params)
        except GravityZoneError as exc:
            log.debug("gz: getCustomGroupsList(%s) failed: %s", pid, exc)
            return
        if not isinstance(result, list):
            return
        for entry in result:
            if not isinstance(entry, dict):
                continue
            gid = entry.get("id")
            name = entry.get("name")
            if isinstance(gid, str) and isinstance(name, str):
                cache[gid] = name
            await _walk(gid if isinstance(gid, str) else None)

    await _walk(parent_id)
    return cache


# ── PHASR row enrichment ────────────────────────────────────────────────────

PHASR_RECOMMENDATION_DEFAULT_GROUP_KEYS: tuple[str, ...] = (
    "category_label",
    "action_taken_label",
    "type_label",
    "resource_name",
    "identity_name",
)
PHASR_RESOURCE_DEFAULT_GROUP_KEYS: tuple[str, ...] = (
    "type",
    "resource_type",
    "resource_name",
)
PHASR_IDENTITY_DEFAULT_GROUP_KEYS: tuple[str, ...] = (
    "type",
    "identity_type",
    "identity_name",
)


def enrich_phasr_recommendation(raw: dict) -> dict:
    """Reverse-code PHASR int enums into human-readable labels.

    The original numeric fields are preserved (``category``,
    ``action_taken``, ``type``) and human-readable counterparts are added
    (``category_label``, ``action_taken_label``, ``type_label``).
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = dict(raw)
    cat = raw.get("category")
    if isinstance(cat, int):
        out["category_label"] = PHASR_CATEGORY_LABELS.get(cat)
    act = raw.get("actionTaken")
    if isinstance(act, int):
        out["action_taken"] = act
        out["action_taken_label"] = PHASR_ACTION_TAKEN_LABELS.get(act)
    typ = raw.get("type")
    if isinstance(typ, int):
        out["type_label"] = PHASR_TYPE_LABELS.get(typ)
    return out


# ── Blocklist row normalisation ─────────────────────────────────────────────

BLOCKLIST_DEFAULT_GROUP_KEYS: tuple[str, ...] = (
    "rule_type",
    "source_info_type",
    "company_id",
)

BLOCKLIST_RULE_TYPE_LABELS: dict[int, str] = {
    1: "hash",
    2: "path",
    3: "connection",
}


def normalize_blocklist_item(raw: dict) -> dict:
    """Add a ``rule_type_label`` derived from the ``ruleType`` int."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = dict(raw)
    rt = raw.get("ruleType")
    if isinstance(rt, int):
        out["rule_type"] = BLOCKLIST_RULE_TYPE_LABELS.get(rt, str(rt))
    return out
