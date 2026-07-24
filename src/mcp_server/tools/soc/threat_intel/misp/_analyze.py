"""gSage AI — MISP analyze dispatcher and helpers.

Each action in ``misp_analyze`` maps to a dispatcher function here.
Pure, stateless helpers that operate on the enriched data from
:class:`._client.MISPClient`.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Any, Optional

from src.mcp_server.tools.soc.threat_intel.misp._client import MISPClient

log = logging.getLogger(__name__)

# ── Dispatch table ────────────────────────────────────────────────────────

ANALYZE_ACTIONS: dict[str, str] = {
    "similarity": "Similarity scoring between an IOC/event and existing events",
    "diff_events": "Compare two events and highlight differences",
    "explain_event": "Generate natural-language summary of an event",
    "suggest_tags": "Suggest tags, galaxies, and ATT&CK for an event",
    "suggest_merge": "Identify duplicate/merge-candidate event pairs",
    "generate_report": "Generate Markdown Event Report from event attributes",
    "correlation_graph": "Build explicit nodes/edges correlation graph",
}


# ── Helper: compute similarity ────────────────────────────────────────────

def compute_similarity(
    source_features: dict,
    target_features: dict,
    strategy: str = "hybrid",
) -> float:
    """Compute similarity score (0-100) between two feature sets.

    Parameters
    ----------
    strategy :
        ``"hybrid"`` — weighted combination (IOCs 40% + ATT&CK 25% + galaxies 20% + tags 15%).
        ``"ioc_only"`` — only IOCs.
        ``"attack_only"`` — only ATT&CK techniques.
        ``"tags_only"`` — tags + galaxies.
    """
    if strategy == "ioc_only":
        weights = {"iocs": 1.0, "attack": 0.0, "galaxies": 0.0, "tags": 0.0}
    elif strategy == "attack_only":
        weights = {"iocs": 0.0, "attack": 1.0, "galaxies": 0.0, "tags": 0.0}
    elif strategy == "tags_only":
        weights = {"iocs": 0.0, "attack": 0.0, "galaxies": 0.5, "tags": 0.5}
    else:  # hybrid
        weights = {"iocs": 0.40, "attack": 0.25, "galaxies": 0.20, "tags": 0.15}

    score = 0.0
    total_weight = sum(weights.values())
    if total_weight == 0:
        return 0.0

    # IOC overlap
    src_iocs = set(source_features.get("iocs", []))
    tgt_iocs = set(target_features.get("iocs", []))
    if src_iocs and tgt_iocs:
        ioc_overlap = len(src_iocs & tgt_iocs) / max(len(src_iocs | tgt_iocs), 1)
        score += weights["iocs"] * ioc_overlap * 100

    # ATT&CK overlap
    src_attack = set(source_features.get("attack_techniques", []))
    tgt_attack = set(target_features.get("attack_techniques", []))
    if src_attack and tgt_attack:
        attack_overlap = len(src_attack & tgt_attack) / max(len(src_attack | tgt_attack), 1)
        score += weights["attack"] * attack_overlap * 100

    # Galaxy overlap
    src_galaxies = set(source_features.get("galaxies", []))
    tgt_galaxies = set(target_features.get("galaxies", []))
    if src_galaxies and tgt_galaxies:
        galaxy_overlap = len(src_galaxies & tgt_galaxies) / max(len(src_galaxies | tgt_galaxies), 1)
        score += weights["galaxies"] * galaxy_overlap * 100

    # Tag overlap
    src_tags = set(source_features.get("tags", []))
    tgt_tags = set(target_features.get("tags", []))
    if src_tags and tgt_tags:
        tag_overlap = len(src_tags & tgt_tags) / max(len(src_tags | tgt_tags), 1)
        score += weights["tags"] * tag_overlap * 100

    return round(score / total_weight, 1)


def extract_features(event: dict) -> dict:
    """Extract feature vector from a normalized event dict."""
    iocs: set[str] = set()
    attack_techniques: set[str] = set()
    galaxies: set[str] = set()
    tags: set[str] = set()

    for attr in event.get("attributes", []):
        val = attr.get("value", "")
        if val:
            iocs.add(val)

    for tech in event.get("attack_techniques", []):
        attack_techniques.add(tech)

    for galaxy in event.get("galaxies", []):
        galaxies.add(f"{galaxy.get('name', '')}:{galaxy.get('cluster', '')}".strip(":"))

    for tag in event.get("tags", []):
        tags.add(tag)

    return {
        "iocs": sorted(iocs),
        "attack_techniques": sorted(attack_techniques),
        "galaxies": sorted(galaxies),
        "tags": sorted(tags),
    }


def build_correlation_graph(
    events: list[dict],
    depth: int = 2,
) -> dict:
    """Build a simplified correlation graph from a list of events.

    Returns ``{"nodes": [...], "edges": [...]}`` suitable for rendering.
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_node_ids: set[str] = set()

    for event in events:
        eid = str(event.get("id", event.get("uuid", "")))
        if not eid or eid in seen_node_ids:
            continue
        seen_node_ids.add(eid)

        # Event node
        nodes.append({
            "id": eid,
            "type": "event",
            "label": event.get("title", f"Event #{event.get('id')}"),
            "threat_level": event.get("threat_level_id"),
        })

        # Attribute nodes + edges
        for attr in event.get("attributes", []):
            aid = f"attr_{attr.get('id', '')}"
            if aid not in seen_node_ids:
                seen_node_ids.add(aid)
                nodes.append({
                    "id": aid,
                    "type": "attribute",
                    "label": f"{attr.get('type')}: {attr.get('value', '')}",
                })
            edges.append({
                "from": eid,
                "to": aid,
                "relation": "contains",
            })

        # Galaxy nodes
        for galaxy in event.get("galaxies", []):
            gid = f"galaxy_{galaxy.get('name', '')}_{galaxy.get('cluster', '')}"
            gid = gid.replace(" ", "_").strip("_")
            if gid not in seen_node_ids:
                seen_node_ids.add(gid)
                nodes.append({
                    "id": gid,
                    "type": "galaxy",
                    "label": f"{galaxy.get('name')}: {galaxy.get('cluster')}",
                })
            edges.append({
                "from": eid,
                "to": gid,
                "relation": "tagged-as",
            })

    return {"nodes": nodes, "edges": edges}
