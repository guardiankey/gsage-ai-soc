"""gSage AI — MISP query helpers and shared configuration.

Normalisers, IOC type detection, severity mapping, enrichment helpers,
and the shared tool config schema. All helpers operate on the JSON-decoded
``dict``/``list`` payloads returned by :class:`._client.MISPClient`.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from src.mcp_server.tools.soc.threat_intel.misp._client import MISPError

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

DEFAULT_MAX_ROWS = 50
HARD_MAX_ROWS = 500
DEFAULT_MAX_EVENTS = 50
HARD_MAX_EVENTS = 200
DEFAULT_MAX_ATTRIBUTES_PER_EVENT = 30
HARD_MAX_ATTRIBUTES_PER_EVENT = 100
DEFAULT_MAX_RELATED_IOCS = 10
HARD_MAX_RELATED_IOCS = 50

# Cache TTLs (seconds)
CACHE_TTL_SEARCH = 300       # 5 min
CACHE_TTL_REFERENCE = 86400  # 24 h
CACHE_TTL_CAPABILITIES = 3600  # 1 h
CACHE_TTL_ANALYZE = 600      # 10 min

# Retry classification — PyMISP exceptions don't carry HTTP status directly,
# but we classify known error patterns.
MISP_RETRYABLE_PATTERNS: frozenset[str] = frozenset({
    "Could not connect", "timed out", "Too Many Requests",
    "Service Unavailable", "Connection reset",
})


def is_retryable_error(exc: "MISPError") -> bool:
    """Classify a MISPError as retryable based on its message content."""
    # Import here to avoid circular dependency
    msg = exc.message.lower()
    return any(pattern.lower() in msg for pattern in MISP_RETRYABLE_PATTERNS)


# ── IOC type detection ─────────────────────────────────────────────────────

# Regex patterns for auto-detecting IOC type from a raw value.
_IOC_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("md5", re.compile(r"^[a-fA-F0-9]{32}$")),
    ("sha1", re.compile(r"^[a-fA-F0-9]{40}$")),
    ("sha256", re.compile(r"^[a-fA-F0-9]{64}$")),
    ("sha512", re.compile(r"^[a-fA-F0-9]{128}$")),
    ("domain", re.compile(
        r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
        r"(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*\.[a-zA-Z]{2,}$"
    )),
    ("url", re.compile(r"^https?://", re.IGNORECASE)),
    ("email", re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")),
    ("email-src", re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")),
    ("filename", re.compile(r"^.+\.(exe|dll|doc|docx|xls|xlsx|pdf|zip|rar|7z|msi|ps1|vbs|js|vbe)$", re.IGNORECASE)),
]


def detect_ioc_type(value: str) -> Optional[str]:
    """Best-effort detection of MISP attribute type from a raw IOC value.

    Returns the detected type string (e.g. ``"ip-dst"``, ``"sha256"``,
    ``"domain"``) or ``None`` if the value cannot be classified.
    """
    if not value or not value.strip():
        return None
    v = value.strip()

    # IP addresses (v4 and v6)
    try:
        ip = ipaddress.ip_address(v)
        if ip.version == 4:
            return "ip-dst"
        else:
            return "ip-dst"
    except ValueError:
        pass

    # Hash patterns
    for type_name, pattern in _IOC_PATTERNS:
        if pattern.match(v):
            return type_name

    # ASN pattern
    if re.match(r"^[aA][sS]\d+$", v):
        return "AS"

    return None


# ── Enrichment helpers ─────────────────────────────────────────────────────

def build_ioc_summary(
    events: list[dict],
    attributes: list[dict],
) -> dict:
    """Build a consolidated threat-intel summary from search results.

    Extracts unique threat actors, malware families, confidence stats
    and temporal window from a set of MISP events and attributes.
    """
    threat_actors: set[str] = set()
    malware_families: set[str] = set()
    attack_techniques: set[str] = set()
    tags: set[str] = set()
    confidences: list[int] = []
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None

    for event in events:
        # Extract galaxies
        for galaxy in event.get("Galaxy", []):
            galaxy_name = galaxy.get("name", "")
            for cluster in galaxy.get("GalaxyCluster", []):
                cluster_value = cluster.get("value", "")
                cluster_type = cluster.get("type", "")
                if cluster_type and cluster_value:
                    if "threat" in cluster_type.lower() or "actor" in cluster_type.lower():
                        threat_actors.add(cluster_value)
                    if "malware" in cluster_type.lower() or "tool" in cluster_type.lower():
                        malware_families.add(cluster_value)

        # Extract tags
        for tag in event.get("Tag", []):
            tag_name = tag.get("name", "")
            if tag_name:
                tags.add(tag_name)
                # MITRE ATT&CK tags
                if tag_name.startswith("misp-galaxy:mitre"):
                    tech = tag_name.split('="')[1].split('"')[0] if '="' in tag_name else ""
                    if tech and tech.startswith("T"):
                        attack_techniques.add(tech)

        # Confidence
        try:
            confidence = int(event.get("Attribute", [{}])[0].get("Tag", [{}])[0] or 0)
        except (IndexError, KeyError, TypeError, ValueError):
            pass

    return {
        "threat_actors": sorted(threat_actors),
        "malware_families": sorted(malware_families),
        "attack_techniques": sorted(attack_techniques),
        "tags_summary": sorted(tags)[:20],
        "confidence_avg": round(sum(confidences) / len(confidences)) if confidences else None,
        "first_seen_overall": first_seen,
        "last_seen_overall": last_seen,
        "event_count": len(events),
        "attribute_count": len(attributes),
    }


def normalize_event(event: dict, max_attributes: int = 30) -> dict:
    """Normalize a raw MISP event dict into a compact agent-friendly shape."""
    org = event.get("Orgc", event.get("Org", {}))
    org_name = org.get("name", "") if isinstance(org, dict) else ""

    # Extract galaxies
    galaxies: list[dict] = []
    for g in event.get("Galaxy", []):
        g_name = g.get("name", "")
        for cluster in g.get("GalaxyCluster", []):
            galaxies.append({
                "name": g_name,
                "cluster": cluster.get("value", ""),
                "description": cluster.get("description", "")[:200],
            })

    # Extract tags
    tag_names: list[str] = []
    attack_techniques: list[str] = []
    for t in event.get("Tag", []):
        tag_name = t.get("name", "")
        if tag_name:
            tag_names.append(tag_name)
            if "misp-galaxy:mitre" in tag_name:
                try:
                    tech = tag_name.split('="')[1].split('"')[0]
                    if tech.startswith("T"):
                        attack_techniques.append(tech)
                except (IndexError, ValueError):
                    pass

    # Extract attributes (capped)
    raw_attrs = event.get("Attribute", [])
    attributes = []
    for attr in raw_attrs[:max_attributes]:
        attributes.append({
            "id": attr.get("id"),
            "type": attr.get("type"),
            "category": attr.get("category"),
            "value": attr.get("value"),
            "to_ids": attr.get("to_ids", False),
            "comment": attr.get("comment", "") or "",
        })

    return {
        "id": event.get("id"),
        "uuid": event.get("uuid"),
        "title": event.get("info", ""),
        "date": event.get("date", ""),
        "publish_timestamp": event.get("publish_timestamp", ""),
        "threat_level_id": event.get("threat_level_id"),
        "analysis": event.get("analysis"),
        "distribution": event.get("distribution"),
        "published": event.get("published", False),
        "org": {"id": org.get("id") if isinstance(org, dict) else None, "name": org_name},
        "tags": tag_names[:20],
        "galaxies": galaxies[:10],
        "attack_techniques": attack_techniques[:20],
        "attribute_count": len(raw_attrs),
        "attributes": attributes,
    }


def normalize_attribute(attr: dict) -> dict:
    """Normalize a raw MISP attribute dict into an agent-friendly shape."""
    return {
        "id": attr.get("id"),
        "event_id": attr.get("event_id"),
        "type": attr.get("type"),
        "category": attr.get("category"),
        "value": attr.get("value"),
        "to_ids": attr.get("to_ids", False),
        "comment": attr.get("comment", "") or "",
        "timestamp": attr.get("timestamp", ""),
        "first_seen": attr.get("first_seen", ""),
        "last_seen": attr.get("last_seen", ""),
    }


# ── MISP Config Schema ─────────────────────────────────────────────────────

MISP_CONFIG_SCHEMA: dict = {
    "type": "object",
    "required": ["url", "api_key"],
    "properties": {
        "url": {
            "type": "string",
            "format": "uri",
            "description": "Base URL of the MISP instance (e.g. https://misp.example.com).",
        },
        "api_key": {
            "type": "string",
            "minLength": 20,
            "description": "MISP API authentication key (AuthKey).",
        },
        "verify_cert": {
            "type": "boolean",
            "default": True,
            "description": "Verify TLS certificate (default: true).",
        },
        "default_distribution": {
            "type": "integer",
            "minimum": 0,
            "maximum": 4,
            "default": 0,
            "description": "Default distribution for new events: 0=YourOrg, 1=Community, 2=Connected, 3=All, 4=SharingGroup.",
        },
        "default_threat_level": {
            "type": "integer",
            "minimum": 1,
            "maximum": 4,
            "default": 2,
            "description": "Default threat level: 1=High, 2=Medium, 3=Low, 4=Undefined.",
        },
        "default_analysis": {
            "type": "integer",
            "minimum": 0,
            "maximum": 2,
            "default": 0,
            "description": "Default analysis level: 0=Initial, 1=Ongoing, 2=Completed.",
        },
    },
    "additionalProperties": False,
}

MISP_CONFIG_DEFAULTS: dict = {
    "default_distribution": 0,
    "default_threat_level": 2,
    "default_analysis": 0,
    "verify_cert": True,
}
