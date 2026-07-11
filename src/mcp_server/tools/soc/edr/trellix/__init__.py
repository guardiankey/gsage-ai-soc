"""gSage AI — Trellix EDR (MVISION/Helix) tools.

Provides async tools for the Trellix EDR platform via the public APIs:

- ``api.manage.trellix.com/edr/v2``       — realtime SQL-like searches.
- ``api.soc.<region>.trellix.com/active-response/api/v1`` — Active Response
  structured searches and remediation actions.

Authentication is OAuth2 client-credentials at
``auth.trellix.com/auth/realms/IAM/protocol/openid-connect/token`` using the
``mcafee`` audience and the SOC scope set
(``mi.user.investigate soc.act.tg soc.hts.c soc.hts.r soc.rts.c soc.rts.r``).

Tools:
    trellix_edr_search                       — Generic v1/v2 search dispatcher.
    trellix_edr_search_files                 — File hunt by name/hash + host.
    trellix_edr_search_bulk_files            — Bulk file hunt by list of hashes or names.
    trellix_edr_search_network               — Network flow hunt.
    trellix_edr_search_bulk_network          — Bulk network flow hunt by list of IPs or process names.
    trellix_edr_search_processes             — Process hunt (Processes/ProcessHistory).
    trellix_edr_quarantine_host              — Host (un)quarantine by hostname/IP.
    trellix_edr_get_host_quarantine_status   — Read quarantine state per host.
    trellix_edr_alerts                       — Fetch and summarise v3 alerts with filtering.
    trellix_edr_threats                      — Fetch threats, affected hosts, and detections.

All tools support multiple Trellix tenants via ``config_profile``
(``supports_multiple_configs=True``).
"""
