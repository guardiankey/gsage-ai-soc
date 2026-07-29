"""gSage AI — MISP dashboard dispatcher and helpers.

Each dashboard view maps to a dispatcher function here. Pure, stateless
helpers that issue bounded MISP queries and return structured aggregations.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.mcp_server.tools.soc.threat_intel.misp._client import MISPClient, MISPError

log = logging.getLogger(__name__)

# ── Dispatch table ────────────────────────────────────────────────────────

DASHBOARD_VIEWS: dict[str, str] = {
    "overview": "Global counts: events, attributes, objects, tags, galaxies, sightings, feeds, orgs",
    "events_timeline": "Time series of events created per day/week/month",
    "top_tags": "Most frequent tags",
    "top_galaxies": "Most referenced galaxies/clusters",
    "top_organisations": "Organisations contributing the most events",
    "threat_level_distribution": "Event distribution by threat level",
    "distribution_map": "Event distribution by sharing level",
    "feed_health": "Feed status: enabled/disabled, last fetch, event count, errors",
    "sightings_trend": "Sightings trend per day, top sighted attributes",
    "attribute_type_distribution": "Distribution of attribute types",
    "warninglist_hits": "Enabled warninglists with event/attribute hit counts",
    "attack_matrix": "MITRE ATT&CK matrix populated from MISP events",
}

DASHBOARD_TOP_N = 10

# ── Cache TTL per view ────────────────────────────────────────────────────

VIEW_CACHE_TTL: dict[str, int] = {
    "overview": 600,
    "events_timeline": 600,
    "top_tags": 600,
    "top_galaxies": 600,
    "top_organisations": 600,
    "threat_level_distribution": 600,
    "distribution_map": 600,
    "feed_health": 120,
    "sightings_trend": 300,
    "attribute_type_distribution": 600,
    "warninglist_hits": 600,
    "attack_matrix": 600,
}


# ── View dispatchers ──────────────────────────────────────────────────────

async def view_overview(
    client: MISPClient,
    window_days: int = 30,
    tag_filter: Optional[str] = None,
    org_id: Optional[int] = None,
) -> dict:
    """Global overview: counts, delta vs previous period."""
    search_kwargs: dict[str, Any] = {"metadata": True, "limit": 1}
    if tag_filter:
        search_kwargs["tags"] = [tag_filter]
    if org_id:
        search_kwargs["org"] = org_id

    # Current window
    date_from = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    # Previous window for delta
    prev_from = (datetime.now(timezone.utc) - timedelta(days=window_days * 2)).strftime("%Y-%m-%d")
    prev_to = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")

    # Published events
    pub_current = await client.search("events", date_from=date_from, published=True, **search_kwargs)
    pub_prev = await client.search("events", date_from=prev_from, date_to=prev_to, published=True, **search_kwargs)

    # Unpublished events
    unpub_current = await client.search("events", date_from=date_from, published=False, **search_kwargs)

    # Feeds
    try:
        feeds = await client.get_feeds_list()
        feeds_list = feeds if isinstance(feeds, list) else feeds.get("response", feeds.get("Feed", []))
        active_feeds = sum(1 for f in feeds_list if isinstance(f, dict) and f.get("Feed", f).get("enabled"))
    except MISPError:
        feeds_list = []
        active_feeds = 0

    current_count = _count_events(pub_current)
    prev_count = _count_events(pub_prev)
    delta_pct = round(((current_count - prev_count) / max(prev_count, 1)) * 100, 1) if prev_count else 0

    return {
        "view": "overview",
        "window_days": window_days,
        "events_published": current_count,
        "events_published_prev_period": prev_count,
        "events_published_delta_pct": delta_pct,
        "events_unpublished": _count_events(unpub_current),
        "feeds_active": active_feeds,
        "feeds_total": len(feeds_list) if isinstance(feeds_list, list) else 0,
    }


async def view_events_timeline(
    client: MISPClient,
    window_days: int = 30,
    granularity: str = "day",
    tag_filter: Optional[str] = None,
    org_id: Optional[int] = None,
) -> dict:
    """Event creation timeline."""
    search_kwargs: dict[str, Any] = {"metadata": True, "limit": 500}
    if tag_filter:
        search_kwargs["tags"] = [tag_filter]
    if org_id:
        search_kwargs["org"] = org_id

    date_from = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")

    result = await client.search("events", date_from=date_from, **search_kwargs)
    raw = result if isinstance(result, list) else result.get("response", result.get("Event", []))

    timeline: dict[str, int] = defaultdict(int)
    for event in raw:
        if not isinstance(event, dict):
            continue
        e = event.get("Event", event)
        date_str = e.get("date", "")
        if not date_str:
            continue
        if granularity == "month":
            key = date_str[:7]  # YYYY-MM
        elif granularity == "week":
            # ISO week
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                key = dt.strftime("%G-W%V")
            except ValueError:
                key = date_str[:7]
        else:
            key = date_str  # YYYY-MM-DD

        timeline[key] += 1

    sorted_timeline = sorted(timeline.items())

    return {
        "view": "events_timeline",
        "window_days": window_days,
        "granularity": granularity,
        "total_events": sum(timeline.values()),
        "timeline": [{"date": k, "count": v} for k, v in sorted_timeline],
    }


async def view_top_tags(
    client: MISPClient,
    top_n: int = 10,
    window_days: int = 30,
) -> dict:
    """Top tags by event count."""
    result = await client.get_tags_list()
    raw = result if isinstance(result, list) else result.get("response", result.get("Tag", []))

    tags: list[dict] = []
    total = 0
    for t in raw:
        if not isinstance(t, dict):
            continue
        tag = t.get("Tag", t)
        count = int(tag.get("count", 0))
        total += count
        tags.append({"name": tag.get("name"), "count": count})

    tags.sort(key=lambda x: x["count"], reverse=True)

    return {
        "view": "top_tags",
        "top_n": top_n,
        "window_days": window_days,
        "total_tagged": total,
        "tags": [{"name": t["name"], "count": t["count"], "pct": round(t["count"] / max(total, 1) * 100, 1)} for t in tags[:top_n]],
    }


async def view_top_galaxies(
    client: MISPClient,
    top_n: int = 10,
    window_days: int = 30,
) -> dict:
    """Top galaxies/clusters by reference count."""
    result = await client.get_galaxies_list()
    raw = result if isinstance(result, list) else result.get("response", [])

    items: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            gc = item.get("GalaxyCluster", item)
            items.append({
                "name": gc.get("value", gc.get("name", "")),
                "type": gc.get("type", ""),
                "description": str(gc.get("description", ""))[:200],
                "tag_count": int(gc.get("tag_count", 0)),
            })

    items.sort(key=lambda x: x["tag_count"], reverse=True)

    return {
        "view": "top_galaxies",
        "top_n": top_n,
        "window_days": window_days,
        "galaxies": items[:top_n],
    }


async def view_top_organisations(
    client: MISPClient,
    top_n: int = 10,
    window_days: int = 30,
) -> dict:
    """Top organisations by event count."""
    result = await client.get_organisations_list()
    raw = result if isinstance(result, list) else result.get("response", [])

    orgs: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            o = item.get("Organisation", item)
            orgs.append({
                "name": o.get("name", ""),
                "local": o.get("local"),
                "uuid": o.get("uuid"),
                "event_count": int(o.get("event_count", 0)),
            })

    orgs.sort(key=lambda x: x["event_count"], reverse=True)

    return {
        "view": "top_organisations",
        "top_n": top_n,
        "window_days": window_days,
        "organisations": orgs[:top_n],
    }


async def view_threat_level_distribution(
    client: MISPClient,
    window_days: int = 30,
) -> dict:
    """Event distribution by threat level."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    result = await client.search("events", date_from=date_from, metadata=True, limit=500)
    raw = result if isinstance(result, list) else result.get("response", result.get("Event", []))

    counter: dict[str, int] = {"High": 0, "Medium": 0, "Low": 0, "Undefined": 0}
    threat_labels = {1: "High", 2: "Medium", 3: "Low", 4: "Undefined"}

    for event in raw:
        if not isinstance(event, dict):
            continue
        e = event.get("Event", event)
        tl = e.get("threat_level_id", 4)
        label = threat_labels.get(int(tl), "Undefined")
        counter[label] += 1

    total = sum(counter.values()) or 1

    return {
        "view": "threat_level_distribution",
        "window_days": window_days,
        "total_events": total,
        "distribution": [
            {"level": k, "count": v, "pct": round(v / total * 100, 1)}
            for k, v in counter.items()
        ],
    }


async def view_distribution_map(
    client: MISPClient,
    window_days: int = 30,
) -> dict:
    """Event distribution by sharing level."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    result = await client.search("events", date_from=date_from, metadata=True, limit=500)
    raw = result if isinstance(result, list) else result.get("response", result.get("Event", []))

    dist_labels = {0: "YourOrg", 1: "Community", 2: "Connected", 3: "All", 4: "SharingGroup"}
    counter: dict[str, int] = defaultdict(int)

    for event in raw:
        if not isinstance(event, dict):
            continue
        e = event.get("Event", event)
        dist = e.get("distribution", 0)
        label = dist_labels.get(int(dist), "Unknown")
        counter[label] += 1

    total = sum(counter.values()) or 1

    return {
        "view": "distribution_map",
        "window_days": window_days,
        "total_events": total,
        "distribution": [
            {"level": k, "count": v, "pct": round(v / total * 100, 1)}
            for k, v in sorted(counter.items())
        ],
    }


async def view_feed_health(client: MISPClient) -> dict:
    """Feed health: status, last fetch, event count, errors."""
    try:
        result = await client.get_feeds_list()
        raw = result if isinstance(result, list) else result.get("response", result.get("Feed", []))
    except MISPError:
        raw = []

    feeds: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        f = item.get("Feed", item)
        feeds.append({
            "id": f.get("id"),
            "name": f.get("name"),
            "provider": f.get("provider"),
            "enabled": f.get("enabled"),
            "url": f.get("url"),
            "caching_enabled": f.get("caching_enabled"),
        })

    return {
        "view": "feed_health",
        "total_feeds": len(feeds),
        "enabled": sum(1 for f in feeds if f.get("enabled")),
        "disabled": sum(1 for f in feeds if not f.get("enabled")),
        "feeds": feeds,
    }


async def view_sightings_trend(
    client: MISPClient,
    window_days: int = 30,
    top_n: int = 10,
) -> dict:
    """Sightings trend per day."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    result = await client.search("sightings", date_from=date_from, limit=500)
    raw = result if isinstance(result, list) else result.get("response", [])

    sighting_types = {0: "Sighting", 1: "False Positive", 2: "Expiration"}
    by_day: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = Counter()

    for s in raw:
        if not isinstance(s, dict):
            continue
        st = s.get("Sighting", s)
        date_str = st.get("date_sighting", "")
        stype = int(st.get("type", 0))
        if date_str:
            by_day[date_str[:10]] += 1
        by_type[sighting_types.get(stype, "Unknown")] += 1

    sorted_days = sorted(by_day.items())

    return {
        "view": "sightings_trend",
        "window_days": window_days,
        "total_sightings": sum(by_day.values()),
        "sightings_by_type": dict(by_type),
        "timeline": [{"date": k, "count": v} for k, v in sorted_days],
    }


async def view_attribute_type_distribution(
    client: MISPClient,
    window_days: int = 30,
    top_n: int = 10,
) -> dict:
    """Distribution of attribute types."""
    # date_from is not a valid parameter for the attributes controller in
    # PyMISP. Instead search events within the window first, then collect
    # event IDs and search attributes scoped to those events.
    date_from = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    events_result = await client.search("events", date_from=date_from, metadata=True, limit=500)
    events_raw = events_result if isinstance(events_result, list) else (
        events_result.get("response", events_result.get("Event", []))
        if isinstance(events_result, dict) else []
    )

    event_ids = []
    for ev in events_raw:
        if isinstance(ev, dict):
            e = ev.get("Event", ev)
            eid = e.get("id")
            if eid:
                event_ids.append(str(eid))

    counter: dict[str, int] = Counter()
    if event_ids:
        # Search attributes for the events in the date window
        result = await client.search("attributes", eventid=event_ids, limit=500)
        # PyMISP's _check_response already unwraps "response" key.
        raw = result if isinstance(result, list) else result.get("Attribute", [])
        if not isinstance(raw, list):
            raw = []

        for attr in raw:
            if isinstance(attr, dict):
                atype = attr.get("Attribute", attr).get("type", "unknown")
                counter[atype] += 1

    total = sum(counter.values()) or 1

    return {
        "view": "attribute_type_distribution",
        "window_days": window_days,
        "total_attributes": total,
        "distribution": [
            {"type": k, "count": v, "pct": round(v / total * 100, 1)}
            for k, v in counter.most_common(top_n)
        ],
    }


async def view_warninglist_hits(client: MISPClient) -> dict:
    """Warninglists with hit counts."""
    try:
        result = await client.get_warninglists()
        raw = result if isinstance(result, list) else result.get("response", [])
    except MISPError:
        raw = []

    items: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        w = item.get("Warninglist", item)
        items.append({
            "name": w.get("name"),
            "enabled": w.get("enabled"),
            "type": w.get("type"),
            "version": w.get("version"),
        })

    return {
        "view": "warninglist_hits",
        "total_warninglists": len(items),
        "enabled": sum(1 for i in items if i.get("enabled")),
        "warninglists": items,
    }


async def view_attack_matrix(
    client: MISPClient,
    window_days: int = 30,
    mitre_domain: str = "enterprise",
) -> dict:
    """MITRE ATT&CK matrix populated from MISP events."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    result = await client.search("events", date_from=date_from, metadata=True, limit=500)
    raw = result if isinstance(result, list) else result.get("response", result.get("Event", []))

    techniques: dict[str, dict] = defaultdict(lambda: {"count": 0, "actors": set(), "malware": set()})

    for event in raw:
        if not isinstance(event, dict):
            continue
        e = event.get("Event", event)

        # Extract ATT&CK tags
        for tag in e.get("Tag", []):
            tag_name = tag.get("name", "") if isinstance(tag, dict) else str(tag)
            if "misp-galaxy:mitre" not in tag_name:
                continue
            try:
                tech = tag_name.split('="')[1].split('"')[0]
                if tech.startswith("T"):
                    techniques[tech]["count"] += 1
            except (IndexError, ValueError):
                continue

        # Extract galaxies
        for galaxy in e.get("Galaxy", []):
            if not isinstance(galaxy, dict):
                continue
            galaxy_name = galaxy.get("name", "")
            for cluster in galaxy.get("GalaxyCluster", []):
                cluster_type = (cluster.get("type") or "").lower()
                cluster_value = cluster.get("value", "")
                if "threat" in cluster_type or "actor" in cluster_type:
                    for tech in list(techniques.keys()):
                        # Associate actor with all techniques in the event
                        if tech in str(e.get("Tag", [])):
                            techniques[tech]["actors"].add(cluster_value)
                if "malware" in cluster_type or "tool" in cluster_type:
                    for tech in list(techniques.keys()):
                        if tech in str(e.get("Tag", [])):
                            techniques[tech]["malware"].add(cluster_value)

    matrix = [
        {
            "technique": tech,
            "count": info["count"],
            "threat_actors": sorted(info["actors"])[:5],
            "malware_families": sorted(info["malware"])[:5],
        }
        for tech, info in sorted(techniques.items(), key=lambda x: x[1]["count"], reverse=True)
    ]

    return {
        "view": "attack_matrix",
        "window_days": window_days,
        "mitre_domain": mitre_domain,
        "techniques_count": len(matrix),
        "techniques": matrix,
    }


# ── Helpers ───────────────────────────────────────────────────────────────

def _count_events(result: dict | list) -> int:
    """Extract event count from a search result."""
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        # Try response metadata
        resp = result.get("response", result)
        if isinstance(resp, list):
            return len(resp)
        if isinstance(resp, dict):
            return int(resp.get("total_count", len(resp.get("Event", []))))
    return 0
