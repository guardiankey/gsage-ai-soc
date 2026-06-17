"""SEI-PEN reference-data caching.

Stable SEI reference lists (process types, document types/séries, legal
hypotheses, units) change rarely but are needed repeatedly by ID-resolution
helpers and the dashboard. They are cached with the platform result cache
(:func:`src.shared.cache.cached`, backed by the PostgreSQL ``gsage_tool_cache``
table) instead of re-hitting the SEI API on every call.

Cache keys embed the SEI ``base_url`` + ``orgao_id`` so different installations
or organs never share entries, on top of the ``org`` scope isolation provided by
the decorator.

The cached helpers require a ``session`` (AsyncSession) and ``org_id`` kwarg; the
calling tool bridges its DB session into ``execute()`` via a ContextVar (see the
``run()`` override in ``sei_read.py`` / ``sei_write.py``). When no session is
available the decorator transparently bypasses the cache and calls through.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.cache import cached, invalidate_org_cache

from custom_code.tools.sei_pen._client import SeiPenClient

log = logging.getLogger(__name__)

_LOGICAL_NAME = "sei_pen"

# Reference data is fairly stable — cache for hours.
_TTL_TYPES = 12 * 3600          # process/document types & séries
_TTL_HIPOTESES = 12 * 3600      # legal hypotheses
_TTL_UNIDADES = 3 * 3600        # units / departments

# Generous page size to fetch a full reference list in one shot.
_REF_LIMIT = 1000


def _as_list(data: Any) -> list[dict]:
    """Normalise a SEI ``data`` payload into a list of dict records."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        # Some endpoints wrap the list under a single key.
        for value in data.values():
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        return [data]
    return []


async def _fetch_list(
    client: SeiPenClient,
    path: str,
    query: dict[str, Any],
    unidade_override: Optional[str],
) -> list[dict]:
    body = await client.request("GET", path, params=query or None, unidade_override=unidade_override)
    return _as_list(body.get("data"))


# ── Cached loaders ────────────────────────────────────────────────────────────


@cached(
    ttl=_TTL_TYPES,
    scope="org",
    key_fn=lambda *, base_url, orgao_id, **_: f"sei:tipos_processo:{base_url}:{orgao_id}",
    logical_name=_LOGICAL_NAME,
)
async def load_tipos_processo(
    *,
    client: SeiPenClient,
    base_url: str,
    orgao_id: str,
    org_id: Optional[uuid.UUID] = None,
    session: Optional[AsyncSession] = None,
) -> list[dict]:
    """Load all process types (id + name). Cached per org + installation."""
    return await _fetch_list(
        client, "/processo/tipo/listar", {"limit": _REF_LIMIT, "start": 0}, None
    )


@cached(
    ttl=_TTL_TYPES,
    scope="org",
    key_fn=lambda *, base_url, orgao_id, unidade, **_: (
        f"sei:series:{base_url}:{orgao_id}:{unidade or ''}"
    ),
    logical_name=_LOGICAL_NAME,
)
async def load_series(
    *,
    client: SeiPenClient,
    base_url: str,
    orgao_id: str,
    unidade: Optional[str],
    org_id: Optional[uuid.UUID] = None,
    session: Optional[AsyncSession] = None,
) -> list[dict]:
    """Load document types/séries (id + name). Cached per org + unit."""
    return await _fetch_list(
        client,
        "/documento/tipo/pesquisar",
        {"limit": _REF_LIMIT, "start": 0},
        unidade,
    )


@cached(
    ttl=_TTL_HIPOTESES,
    scope="org",
    key_fn=lambda *, base_url, orgao_id, nivel_acesso, **_: (
        f"sei:hipoteses:{base_url}:{orgao_id}:{nivel_acesso}"
    ),
    logical_name=_LOGICAL_NAME,
)
async def load_hipoteses(
    *,
    client: SeiPenClient,
    base_url: str,
    orgao_id: str,
    nivel_acesso: int,
    org_id: Optional[uuid.UUID] = None,
    session: Optional[AsyncSession] = None,
) -> list[dict]:
    """Load legal hypotheses for a given access level. Cached per org + level."""
    return await _fetch_list(
        client,
        "/hipoteseLegal/pesquisar",
        {"limit": _REF_LIMIT, "start": 0, "nivelAcesso": nivel_acesso},
        None,
    )


@cached(
    ttl=_TTL_UNIDADES,
    scope="org",
    key_fn=lambda *, base_url, orgao_id, **_: f"sei:unidades:{base_url}:{orgao_id}",
    logical_name=_LOGICAL_NAME,
)
async def load_unidades(
    *,
    client: SeiPenClient,
    base_url: str,
    orgao_id: str,
    org_id: Optional[uuid.UUID] = None,
    session: Optional[AsyncSession] = None,
) -> list[dict]:
    """Load organizational units. Cached per org + installation."""
    return await _fetch_list(
        client, "/unidade/pesquisar", {"limit": _REF_LIMIT, "start": 0}, None
    )


async def invalidate_reference_cache(
    *, org_id: uuid.UUID, session: AsyncSession
) -> int:
    """Drop all cached SEI reference data for an organization.

    Call after the user changes SEI configuration (base_url / orgao_id) so stale
    type/série/hypothesis lists are not reused.
    """
    return await invalidate_org_cache(
        session, org_id=org_id, tool_name=_LOGICAL_NAME
    )
