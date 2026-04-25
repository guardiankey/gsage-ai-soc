"""ops_cli internal helpers (org resolution, JSON output)."""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_org_id(
    db: AsyncSession,
    *,
    org_id: Optional[str],
    org_slug: Optional[str],
) -> uuid.UUID:
    """Return the organization UUID for ``--org-id`` or ``--org-slug``.

    If neither is provided, and there is exactly one organization in the
    database, that one is returned.  Otherwise raises ``ValueError``.
    """
    from src.shared.models import GSageOrganization  # noqa: PLC0415

    if org_id:
        try:
            return uuid.UUID(org_id)
        except ValueError as exc:
            raise ValueError(f"--org-id is not a valid UUID: {org_id}") from exc

    if org_slug:
        result = await db.execute(
            select(GSageOrganization).where(GSageOrganization.slug == org_slug)
        )
        org = result.scalar_one_or_none()
        if org is None:
            raise ValueError(f"Organization with slug {org_slug!r} not found")
        return org.id

    # Fall back: if only one org exists, pick it.
    result = await db.execute(select(GSageOrganization))
    orgs = result.scalars().all()
    if len(orgs) == 1:
        return orgs[0].id
    if not orgs:
        raise ValueError("No organization exists yet — run the bootstrap admin first")
    names = ", ".join(f"{o.slug}" for o in orgs)
    raise ValueError(
        f"Multiple organizations exist ({names}). "
        "Pass --org-slug <slug> or --org-id <uuid>."
    )


def print_result(payload: dict[str, Any], *, json_out: bool) -> None:
    if json_out:
        json.dump(payload, sys.stdout, default=str, indent=2)
        sys.stdout.write("\n")
        return

    status = payload.get("status", "ok")
    message = payload.get("message", "")
    print(f"[{status}] {message}" if message else f"[{status}]")
    details = payload.get("details")
    if isinstance(details, dict):
        for k, v in details.items():
            print(f"  {k}: {v}")
