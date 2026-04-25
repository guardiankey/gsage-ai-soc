"""Curator — seed default collections on startup.

Creates the five default collections if they do not already exist
(matched by slug).  Safe to call multiple times (idempotent).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Collection, _make_slug

log = logging.getLogger(__name__)

# (short_description, subtype, type, description)
_SEED_COLLECTIONS = [
    (
        "proxy",
        None,
        "ip",
        "IP addresses associated with proxy servers (open proxies, VPN exit nodes, Tor exits).",
    ),
    (
        "proxy",
        None,
        "domain",
        "Domain names associated with proxy services.",
    ),
    (
        "proxy",
        None,
        "url",
        "URLs associated with proxy services.",
    ),
    (
        "email",
        "smtp_servers",
        "ip",
        "IP addresses of known email SMTP servers (legitimate senders infrastructure).",
    ),
    (
        "email",
        "senders",
        "email",
        "Email addresses of known senders (blocklist/allowlist/suspected).",
    ),
]


async def run_seed(session: AsyncSession) -> None:
    """Create seed collections that do not yet exist."""
    for short_desc, subtype, col_type, description in _SEED_COLLECTIONS:
        slug = _make_slug(short_desc, subtype, col_type)
        result = await session.execute(
            select(Collection).where(Collection.slug == slug).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            continue

        collection = Collection(
            short_description=short_desc,
            description=description,
            slug=slug,
            type=col_type,
            subtype=subtype,
            active=True,
            status="idle",
        )
        session.add(collection)
        log.info("seed: created collection slug=%s", slug)

    await session.commit()
