"""gSage AI — E-goi marketing platform tool family.

Wraps the official synchronous `egoi_api` SDK (E-goi API v3) behind a thin
async facade. Each public coroutine wraps the blocking call in
:func:`asyncio.to_thread` so the tool layer stays async-friendly. Errors
from the SDK are normalised into :class:`._client.EgoiError`.

Tools
-----

Read tools (no approval, ``egoi:read``):

* ``egoi_list_search`` — list/detail mailing lists
* ``egoi_contact_search`` — global or per-list contact search
* ``egoi_contact_get`` — single contact detail
* ``egoi_campaign_search`` — list/detail campaigns
* ``egoi_campaign_report`` — email campaign report (with optional Mermaid chart)
* ``egoi_campaign_group_search`` — list/detail campaign groups
* ``egoi_dashboard`` — multi-view aggregated dashboard

Write tools:

* ``egoi_list_manage`` — create lists (no approval).
* ``egoi_contact_quick_action`` — single / small-batch (≤10) contact
  writes (no approval, ``egoi:write``).
* ``egoi_contact_manage`` — bulk + destructive contact actions
  (approval-gated, ``egoi:write``).

Configuration is multi-tenant: every tool declares
``supports_multiple_configs=True`` and pulls credentials from the admin
console at run-time (see :data:`._query.EGOI_CONFIG_SCHEMA`).
"""
