"""gSage AI — Dashboard view helpers for ``egoi_dashboard``.

Each ``view_*`` coroutine takes an open :class:`EgoiClient` and returns
a serialisable dict ready to be merged into the tool's success payload.
The orchestrator (``egoi_dashboard.py``) dispatches by view name.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from src.mcp_server.tools.marketing.egoi import _query as Q
from src.mcp_server.tools.marketing.egoi._client import EgoiClient

log = logging.getLogger(__name__)


# Maximum items fetched per dashboard subsection. Dashboards are
# summary surfaces, not full enumerations — agents should drill down
# with dedicated search tools when needed.
DASHBOARD_TOP_N = 10
DASHBOARD_MAX_ROWS = 200


async def _hydrate_list_stats(
    client: EgoiClient, lists: list[dict]
) -> list[dict]:
    """Fan out ``get_list`` per row to populate ``contact_stats``.

    The ``/lists`` endpoint omits stats, so the dashboard needs an
    extra call per list. Failures degrade silently (None values stay).
    """
    if not lists:
        return lists

    async def _one(row: dict) -> dict:
        lid = row.get("list_id")
        if not isinstance(lid, int):
            try:
                lid = int(lid)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return row
        try:
            detail = await client.get_list(lid)
        except Exception:  # noqa: BLE001
            return row
        normalised = Q.normalize_list(detail) if isinstance(detail, dict) else {}
        for key in (
            "contacts_active",
            "contacts_inactive",
            "contacts_unconfirmed",
            "contacts_removed",
        ):
            if normalised.get(key) is not None:
                row[key] = normalised[key]
        return row

    return list(await asyncio.gather(*(_one(r) for r in lists)))


async def view_overview(client: EgoiClient) -> dict:
    """High-level tenant overview: list count, contact totals, campaign count."""
    lists_payload = await client.get_all_lists(offset=0, limit=DASHBOARD_MAX_ROWS)
    lists = [Q.normalize_list(x) for x in Q.unwrap_items(lists_payload)]
    lists = await _hydrate_list_stats(client, lists)
    contacts_active = sum(int(l.get("contacts_active") or 0) for l in lists)
    contacts_inactive = sum(int(l.get("contacts_inactive") or 0) for l in lists)
    contacts_unconfirmed = sum(int(l.get("contacts_unconfirmed") or 0) for l in lists)
    contacts_removed = sum(int(l.get("contacts_removed") or 0) for l in lists)
    campaigns_payload = await client.get_all_campaigns(offset=0, limit=1)
    campaigns_total = Q.total_items(campaigns_payload) or len(
        Q.unwrap_items(campaigns_payload)
    )
    groups_payload = await client.get_all_campaign_groups(offset=0, limit=1)
    groups_total = Q.total_items(groups_payload) or len(
        Q.unwrap_items(groups_payload)
    )
    return {
        "lists_total": Q.total_items(lists_payload) or len(lists),
        "contacts_active_total": contacts_active,
        "contacts_inactive_total": contacts_inactive,
        "contacts_unconfirmed_total": contacts_unconfirmed,
        "contacts_removed_total": contacts_removed,
        "campaigns_total": campaigns_total,
        "campaign_groups_total": groups_total,
    }


async def view_top_lists(
    client: EgoiClient, *, top_n: int = DASHBOARD_TOP_N
) -> dict:
    """Top lists ranked by active-contact count."""
    payload = await client.get_all_lists(offset=0, limit=DASHBOARD_MAX_ROWS)
    lists = [Q.normalize_list(x) for x in Q.unwrap_items(payload)]
    lists = await _hydrate_list_stats(client, lists)
    ranked = sorted(
        lists, key=lambda r: int(r.get("contacts_active") or 0), reverse=True
    )
    return {"top_lists": ranked[: max(1, top_n)]}


async def view_recent_campaigns(
    client: EgoiClient, *, top_n: int = DASHBOARD_TOP_N
) -> dict:
    """Most recently updated campaigns (sorted client-side)."""
    payload = await client.get_all_campaigns(offset=0, limit=DASHBOARD_MAX_ROWS)
    campaigns = [Q.normalize_campaign(x) for x in Q.unwrap_items(payload)]
    campaigns.sort(key=lambda r: (r.get("updated") or ""), reverse=True)
    return {"recent_campaigns": campaigns[: max(1, top_n)]}


async def view_delivery_funnel(
    client: EgoiClient, *, campaign_hash: Optional[str] = None
) -> dict:
    """Funnel ``sent → delivered → opens → clicks`` for one campaign.

    If ``campaign_hash`` is not given, picks the most-recently updated
    email campaign with a usable status.
    """
    target = campaign_hash
    if not target:
        camp_payload = await client.get_all_campaigns(offset=0, limit=50)
        candidates = sorted(
            (r for r in Q.unwrap_items(camp_payload) if isinstance(r, dict)),
            key=lambda r: (r.get("updated") or ""),
            reverse=True,
        )
        for raw in candidates:
            if raw.get("campaign_hash"):
                target = str(raw.get("campaign_hash"))
                break
    if not target:
        return {"campaign_hash": None, "funnel": {}}
    report = await client.get_email_report(campaign_hash=target)
    totals = (report.get("totals") if isinstance(report, dict) else {}) or {}
    funnel = {
        k: totals.get(k)
        for k in ("sent", "delivered", "opens", "unique_opens", "clicks", "unique_clicks")
        if isinstance(totals.get(k), (int, float))
    }
    return {"campaign_hash": target, "funnel": funnel}


async def view_engagement_trend(
    client: EgoiClient, *, campaign_hash: Optional[str] = None
) -> dict:
    """Per-day opens/clicks for the chosen campaign (raw rows for charting)."""
    target = campaign_hash
    if not target:
        camp_payload = await client.get_all_campaigns(offset=0, limit=50)
        candidates = sorted(
            (r for r in Q.unwrap_items(camp_payload) if isinstance(r, dict)),
            key=lambda r: (r.get("updated") or ""),
            reverse=True,
        )
        for raw in candidates:
            if raw.get("campaign_hash"):
                target = str(raw.get("campaign_hash"))
                break
    if not target:
        return {"campaign_hash": None, "rows": []}
    report = await client.get_email_report(campaign_hash=target)
    rows = list(Q.iter_email_breakdown(report, "by_date"))
    return {"campaign_hash": target, "rows": rows}


# Public dispatch map used by ``egoi_dashboard.py``.
VIEW_DISPATCH = {
    "overview": view_overview,
    "top_lists": view_top_lists,
    "recent_campaigns": view_recent_campaigns,
    "delivery_funnel": view_delivery_funnel,
    "engagement_trend": view_engagement_trend,
}

DASHBOARD_VIEWS: list[str] = list(VIEW_DISPATCH.keys())
