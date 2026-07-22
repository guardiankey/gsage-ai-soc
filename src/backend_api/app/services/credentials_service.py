"""gSage AI — Service layer for the per-user credentials keychain."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.backend_api.app.schemas.credentials import (
    CredentialIn,
    CredentialUpdate,
    ToolLinkIn,
)
from src.shared.models.user_credential import (
    GSageUserCredential,
    GSageUserCredentialToolLink,
)

logger = logging.getLogger(__name__)


# Sensitive fields that map 1:1 to encrypted columns on GSageUserCredential.
_SENSITIVE_SCALAR_FIELDS = ("username", "password", "domain", "token", "refresh_token")


# ── Read helpers ────────────────────────────────────────────────────────────


async def list_user_credentials(
    db: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
) -> list[GSageUserCredential]:
    """Return all credentials for *user_id* in *org_id*, eager-loading links."""
    stmt = (
        select(GSageUserCredential)
        .where(
            GSageUserCredential.user_id == user_id,
            GSageUserCredential.org_id == org_id,
        )
        .options(selectinload(GSageUserCredential.tool_links))
        .order_by(GSageUserCredential.created_at.desc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_credential(
    db: AsyncSession,
    cred_id: uuid.UUID,
    user_id: uuid.UUID,
) -> GSageUserCredential:
    """Fetch a single credential, enforcing user ownership; 404 if not found."""
    stmt = (
        select(GSageUserCredential)
        .where(
            GSageUserCredential.id == cred_id,
            GSageUserCredential.user_id == user_id,
        )
        .options(selectinload(GSageUserCredential.tool_links))
    )
    result = await db.execute(stmt)
    cred = result.scalar_one_or_none()
    if cred is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found")
    return cred


# ── Mutations ───────────────────────────────────────────────────────────────


def _apply_sensitive_fields(
    cred: GSageUserCredential,
    data: dict,
    *,
    partial: bool,
) -> None:
    """Write sensitive fields onto *cred* via the encryption-aware setters.

    When ``partial=True`` (PUT) only keys present in *data* are touched —
    this preserves values that the caller did not include. An explicit empty
    string clears the stored value (consistent with the email_account model
    pattern).
    """
    for field in _SENSITIVE_SCALAR_FIELDS:
        if not partial or field in data:
            setattr(cred, field, data.get(field))

    if not partial or "extra_fields" in data:
        cred.extra_fields = data.get("extra_fields")


async def create_credential(
    db: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    payload: CredentialIn,
) -> GSageUserCredential:
    """Insert a new credential plus any inline ``tool_links``.

    Encryption is performed by the hybrid setters on the model. The unique
    ``(user_id, label)`` constraint and the partial-unique active-per-tool
    index are mapped to ``409 Conflict`` errors.
    """
    payload_data = payload.model_dump(exclude={"tool_links"})

    cred = GSageUserCredential(
        user_id=user_id,
        org_id=org_id,
        label=payload_data["label"],
        kind=payload.kind.value,
        token_expires_at=payload_data.get("token_expires_at"),
    )
    _apply_sensitive_fields(cred, payload_data, partial=False)
    db.add(cred)

    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        logger.warning("Credential create conflict for user=%s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A credential with this label already exists.",
        ) from exc

    # Inline tool links — apply one-at-a-time so the active-per-tool unique
    # index surfaces as a clean 409 with the offending tool name.
    for link_in in payload.tool_links:
        await _attach_link(db, cred, user_id, link_in)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another credential is already active for one of the requested tools.",
        ) from exc

    await db.refresh(cred, attribute_names=["tool_links"])
    return cred


async def update_credential(
    db: AsyncSession,
    cred_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: CredentialUpdate,
) -> GSageUserCredential:
    """Partial update — only fields explicitly supplied are modified."""
    cred = await get_credential(db, cred_id, user_id)

    data = payload.model_dump(exclude_unset=True)
    if "label" in data:
        cred.label = data["label"]
    if "kind" in data and data["kind"] is not None:
        cred.kind = (
            data["kind"].value if hasattr(data["kind"], "value") else data["kind"]
        )
    if "token_expires_at" in data:
        cred.token_expires_at = data["token_expires_at"]

    _apply_sensitive_fields(cred, data, partial=True)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A credential with this label already exists.",
        ) from exc

    await db.refresh(cred, attribute_names=["tool_links"])
    return cred


async def delete_credential(
    db: AsyncSession,
    cred_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Hard delete — links cascade via ``ondelete='CASCADE'``."""
    cred = await get_credential(db, cred_id, user_id)
    await db.delete(cred)
    await db.commit()


# ── Tool links ──────────────────────────────────────────────────────────────


async def _attach_link(
    db: AsyncSession,
    cred: GSageUserCredential,
    user_id: uuid.UUID,
    payload: ToolLinkIn,
) -> GSageUserCredentialToolLink:
    """Internal: append a link and (when active) deactivate competing ones."""
    if payload.is_active:
        await _deactivate_other_links_for_tool(
            db, user_id=user_id, tool_name=payload.tool_name, except_credential_id=cred.id
        )
    link = GSageUserCredentialToolLink(
        credential_id=cred.id,
        user_id=user_id,
        tool_name=payload.tool_name,
        is_active=payload.is_active,
    )
    db.add(link)
    await db.flush()
    return link


async def link_tool(
    db: AsyncSession,
    cred_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: ToolLinkIn,
) -> GSageUserCredentialToolLink:
    cred = await get_credential(db, cred_id, user_id)
    try:
        link = await _attach_link(db, cred, user_id, payload)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Tool '{payload.tool_name}' is already linked to this credential, "
                "or another credential is already active for this tool."
            ),
        ) from exc
    return link


async def unlink_tool(
    db: AsyncSession,
    cred_id: uuid.UUID,
    link_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    stmt = select(GSageUserCredentialToolLink).where(
        GSageUserCredentialToolLink.id == link_id,
        GSageUserCredentialToolLink.credential_id == cred_id,
        GSageUserCredentialToolLink.user_id == user_id,
    )
    result = await db.execute(stmt)
    link = result.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found")
    await db.delete(link)
    await db.commit()


async def set_active_link(
    db: AsyncSession,
    cred_id: uuid.UUID,
    link_id: uuid.UUID,
    user_id: uuid.UUID,
) -> GSageUserCredentialToolLink:
    """Make ``link_id`` the active credential for its ``(user, tool_name)``.

    Performed transactionally: any other active link for the same
    ``(user_id, tool_name)`` is set to ``is_active=false`` first, then the
    target link is flipped to ``is_active=true``.
    """
    stmt = select(GSageUserCredentialToolLink).where(
        GSageUserCredentialToolLink.id == link_id,
        GSageUserCredentialToolLink.credential_id == cred_id,
        GSageUserCredentialToolLink.user_id == user_id,
    )
    result = await db.execute(stmt)
    link = result.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found")

    await _deactivate_other_links_for_tool(
        db,
        user_id=user_id,
        tool_name=link.tool_name,
        except_credential_id=link.credential_id,
    )
    link.is_active = True
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another credential is already active for this tool.",
        ) from exc
    return link


async def _deactivate_other_links_for_tool(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tool_name: str,
    except_credential_id: uuid.UUID,
) -> None:
    """Set ``is_active=false`` on every active link for ``(user, tool)`` whose
    credential differs from *except_credential_id*."""
    await db.execute(
        update(GSageUserCredentialToolLink)
        .where(
            GSageUserCredentialToolLink.user_id == user_id,
            GSageUserCredentialToolLink.tool_name == tool_name,
            GSageUserCredentialToolLink.is_active.is_(True),
            GSageUserCredentialToolLink.credential_id != except_credential_id,
        )
        .values(is_active=False)
    )


async def set_inactive_link(
    db: AsyncSession,
    cred_id: uuid.UUID,
    link_id: uuid.UUID,
    user_id: uuid.UUID,
) -> GSageUserCredentialToolLink:
    """Deactivate a specific tool link by setting ``is_active=false``."""
    stmt = select(GSageUserCredentialToolLink).where(
        GSageUserCredentialToolLink.id == link_id,
        GSageUserCredentialToolLink.credential_id == cred_id,
        GSageUserCredentialToolLink.user_id == user_id,
    )
    result = await db.execute(stmt)
    link = result.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found")

    link.is_active = False
    await db.commit()
    return link


# ── Runtime resolution (consumed by agent proxy) ────────────────────────────


async def resolve_active_for_tool(
    db: AsyncSession,
    user_id: uuid.UUID,
    tool_name: str,
) -> Optional[dict]:
    """Return the decrypted credential dict for the active link, or ``None``.

    Also updates ``last_used_at`` on the credential (best-effort — failures
    are logged but never raised, since this runs inside the agent loop).
    """
    stmt = (
        select(GSageUserCredential)
        .join(
            GSageUserCredentialToolLink,
            GSageUserCredentialToolLink.credential_id == GSageUserCredential.id,
        )
        .where(
            GSageUserCredentialToolLink.user_id == user_id,
            GSageUserCredentialToolLink.tool_name == tool_name,
            GSageUserCredentialToolLink.is_active.is_(True),
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    cred = result.scalar_one_or_none()
    if cred is None:
        return None

    runtime = cred.to_runtime_dict()
    runtime["credential_id"] = str(cred.id)

    try:
        cred.last_used_at = datetime.now(timezone.utc)
        await db.commit()
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning(
            "Failed to update last_used_at for credential %s: %s", cred.id, exc
        )
        await db.rollback()

    return runtime
