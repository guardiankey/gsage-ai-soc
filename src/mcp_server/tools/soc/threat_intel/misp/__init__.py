"""gSage AI — MISP threat intelligence integration tools.

Wraps the synchronous `pymisp` library behind a thin async facade.
Each public coroutine wraps the blocking call in :func:`asyncio.to_thread`
so the tool layer stays async-friendly. Errors from PyMISP are normalised
into :class:`._client.MISPError`.

Tools
-----

Read tools (no approval, ``misp:read``):

* ``misp_search`` — hybrid unified search: events, attributes, IOCs, tags, galaxies
* ``misp_analyze`` — intelligent analysis: similarity, diff, explanation, suggestions, graphs
* ``misp_dashboard`` — managerial aggregations and statistics

Write tools:

* ``misp_manage`` — create/edit/delete events, attributes, objects, tags, sightings
  (approval-gated, ``misp:write``).

Configuration is multi-tenant: every tool declares
``supports_multiple_configs=True`` and pulls credentials from the admin
console at run-time (see :data:`._query.MISP_CONFIG_SCHEMA`).
"""
