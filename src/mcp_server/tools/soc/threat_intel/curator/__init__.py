"""gSage AI — Curator reputation list management tools.

Provides async tools for managing reputation lists via the Curator microservice
(internal Docker service at http://curator:8000).

Tools:
    curator_lists   — Read: list collections, view items (no approval)
    curator_manage  — Write: add items, delete items, create/update collections
                      (requires curator:write + human-in-the-loop approval)

All tools authenticate with the Curator service via X-API-Key header.
The API key and base URL are configurable per-org via the tool config schema.
"""
