"""gSage AI — Per-user credential keychain routes.

All endpoints are scoped to the authenticated user (``membership.user_id``)
and the URL-scoped ``org_id``. Sensitive credential fields (password,
token, refresh_token, extra_fields) are accepted in plaintext on the wire
(HTTPS) and stored encrypted via :class:`~src.shared.security.encryption.FieldEncryption`.
They are **never** returned by any endpoint — the ``CredentialOut`` schema
exposes only ``has_*`` flags.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import (
    get_db,
    get_org_membership,
    require_permission,
)
from src.backend_api.app.schemas.credentials import (
    AvailableToolOut,
    CredentialIn,
    CredentialOut,
    CredentialUpdate,
    ToolLinkIn,
    ToolLinkOut,
)
from src.backend_api.app.services import credentials_service
from src.shared.models.user_credential import GSageUserCredential
from src.shared.models.user_organization import GSageUserOrganization


router = APIRouter()

logger = logging.getLogger(__name__)

# Permission gate applied to every endpoint.
_PERM = Depends(require_permission("credentials:personal"))


# ---------------------------------------------------------------------------
# Serialization helper
# ---------------------------------------------------------------------------


def _to_out(cred: GSageUserCredential) -> CredentialOut:
    """Build a read model from an ORM credential.

    Username and domain are decrypted and exposed in plaintext (the user
    needs them visible to edit). Password, token, refresh_token, and
    extra_fields values stay encrypted and are surfaced only as ``has_*``
    flags or as the key list for ``extra_fields``.
    """
    extra_keys: list[str] = []
    if cred.has_field("extra_fields"):
        # Decrypting here is unavoidable to expose key names — values stay hidden.
        ef = cred.extra_fields
        if isinstance(ef, dict):
            extra_keys = list(ef.keys())

    return CredentialOut(
        id=cred.id,
        user_id=cred.user_id,
        org_id=cred.org_id,
        label=cred.label,
        kind=cred.kind,  # type: ignore[arg-type]  # validated by enum
        username=cred.username if cred.has_field("username") else None,
        domain=cred.domain if cred.has_field("domain") else None,
        has_username=cred.has_field("username"),
        has_password=cred.has_field("password"),
        has_domain=cred.has_field("domain"),
        has_token=cred.has_field("token"),
        has_refresh_token=cred.has_field("refresh_token"),
        has_extra_fields=cred.has_field("extra_fields"),
        extra_fields_keys=extra_keys,
        token_expires_at=cred.token_expires_at,
        last_used_at=cred.last_used_at,
        created_at=cred.created_at,
        updated_at=cred.updated_at,
        tool_links=[ToolLinkOut.model_validate(link) for link in cred.tool_links],
    )


# ---------------------------------------------------------------------------
# Credential CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[CredentialOut], summary="List my credentials")
async def list_my_credentials(
    org_id: uuid.UUID,
    membership: Annotated[GSageUserOrganization, Depends(get_org_membership)],
    _perm: Annotated[None, _PERM],
    db: AsyncSession = Depends(get_db),
) -> list[CredentialOut]:
    """Return every credential owned by the caller within ``org_id``."""
    creds = await credentials_service.list_user_credentials(
        db, user_id=membership.user_id, org_id=org_id
    )
    return [_to_out(c) for c in creds]


@router.post(
    "",
    response_model=CredentialOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a personal credential",
)
async def create_my_credential(
    org_id: uuid.UUID,
    payload: CredentialIn,
    membership: Annotated[GSageUserOrganization, Depends(get_org_membership)],
    _perm: Annotated[None, _PERM],
    db: AsyncSession = Depends(get_db),
) -> CredentialOut:
    cred = await credentials_service.create_credential(
        db, user_id=membership.user_id, org_id=org_id, payload=payload
    )
    return _to_out(cred)


# ---------------------------------------------------------------------------
# Tools available for credential linking
# (declared BEFORE ``/{cred_id}`` so FastAPI does not match it as a UUID path).
# ---------------------------------------------------------------------------


@router.get(
    "/available-tools",
    response_model=list[AvailableToolOut],
    summary="List tools that accept user credentials",
)
async def list_available_tools(
    org_id: uuid.UUID,
    membership: Annotated[GSageUserOrganization, Depends(get_org_membership)],
    _perm: Annotated[None, _PERM],
    db: AsyncSession = Depends(get_db),
) -> list[AvailableToolOut]:
    """Return distinct credential namespaces that a personal credential can be
    linked to, by querying the ``gsage_tools`` table synced by the MCP server.

    Tools sharing a ``credential_namespace`` (e.g. ``sei_pen_read`` and
    ``sei_pen_write`` both with namespace ``sei_pen``) are collapsed into
    a single entry whose ``name`` is the namespace.

    The list is intentionally **unfiltered** by user permissions: a user
    can pre-create credentials for tools they may later be granted access
    to.  Permission gating still applies at tool execution time.
    """
    from sqlalchemy import select as _select
    from src.shared.models.tool import GSageTool

    result = await db.execute(
        _select(GSageTool).where(
            GSageTool.requires_user_credentials.is_(True),
            GSageTool.is_active.is_(True),
        )
    )
    tools = result.scalars().all()
    logger.info("available-tools: found %d active tools with requires_user_credentials", len(tools))

    grouped: dict[str, dict] = {}
    for tool in tools:
        namespace = tool.credential_namespace or tool.name
        bucket = grouped.setdefault(namespace, {
            "name": namespace,
            "summary": tool.summary or "",
            "category": tool.category or "",
            "credential_schema": tool.credential_schema,
            "members": [],
        })
        bucket["members"].append(tool.name)

    out: list[AvailableToolOut] = []
    for namespace, bucket in grouped.items():
        members = sorted(set(bucket["members"]))
        if len(members) > 1 or members != [namespace]:
            member_hint = f" \u2014 shared by: {', '.join(members)}"
        else:
            member_hint = ""
        out.append(
            AvailableToolOut(
                name=namespace,
                summary=(bucket["summary"] + member_hint).strip(),
                category=bucket["category"],
                credential_schema=bucket["credential_schema"],
            )
        )
    out.sort(key=lambda x: (x.category, x.name))
    return out


@router.get(
    "/{cred_id}",
    response_model=CredentialOut,
    summary="Get a personal credential",
)
async def get_my_credential(
    org_id: uuid.UUID,
    cred_id: uuid.UUID,
    membership: Annotated[GSageUserOrganization, Depends(get_org_membership)],
    _perm: Annotated[None, _PERM],
    db: AsyncSession = Depends(get_db),
) -> CredentialOut:
    cred = await credentials_service.get_credential(
        db, cred_id=cred_id, user_id=membership.user_id
    )
    return _to_out(cred)


@router.put(
    "/{cred_id}",
    response_model=CredentialOut,
    summary="Update a personal credential",
)
async def update_my_credential(
    org_id: uuid.UUID,
    cred_id: uuid.UUID,
    payload: CredentialUpdate,
    membership: Annotated[GSageUserOrganization, Depends(get_org_membership)],
    _perm: Annotated[None, _PERM],
    db: AsyncSession = Depends(get_db),
) -> CredentialOut:
    cred = await credentials_service.update_credential(
        db, cred_id=cred_id, user_id=membership.user_id, payload=payload
    )
    return _to_out(cred)


@router.delete(
    "/{cred_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a personal credential",
)
async def delete_my_credential(
    org_id: uuid.UUID,
    cred_id: uuid.UUID,
    membership: Annotated[GSageUserOrganization, Depends(get_org_membership)],
    _perm: Annotated[None, _PERM],
    db: AsyncSession = Depends(get_db),
) -> None:
    await credentials_service.delete_credential(
        db, cred_id=cred_id, user_id=membership.user_id
    )


# ---------------------------------------------------------------------------
# Tool links
# ---------------------------------------------------------------------------


@router.post(
    "/{cred_id}/links",
    response_model=ToolLinkOut,
    status_code=status.HTTP_201_CREATED,
    summary="Link a credential to a tool",
)
async def link_credential_to_tool(
    org_id: uuid.UUID,
    cred_id: uuid.UUID,
    payload: ToolLinkIn,
    membership: Annotated[GSageUserOrganization, Depends(get_org_membership)],
    _perm: Annotated[None, _PERM],
    db: AsyncSession = Depends(get_db),
) -> ToolLinkOut:
    link = await credentials_service.link_tool(
        db, cred_id=cred_id, user_id=membership.user_id, payload=payload
    )
    return ToolLinkOut.model_validate(link)


@router.delete(
    "/{cred_id}/links/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unlink a credential from a tool",
)
async def unlink_credential_from_tool(
    org_id: uuid.UUID,
    cred_id: uuid.UUID,
    link_id: uuid.UUID,
    membership: Annotated[GSageUserOrganization, Depends(get_org_membership)],
    _perm: Annotated[None, _PERM],
    db: AsyncSession = Depends(get_db),
) -> None:
    await credentials_service.unlink_tool(
        db, cred_id=cred_id, link_id=link_id, user_id=membership.user_id
    )


@router.post(
    "/{cred_id}/links/{link_id}/activate",
    response_model=ToolLinkOut,
    summary="Make this credential the active one for the linked tool",
)
async def activate_credential_link(
    org_id: uuid.UUID,
    cred_id: uuid.UUID,
    link_id: uuid.UUID,
    membership: Annotated[GSageUserOrganization, Depends(get_org_membership)],
    _perm: Annotated[None, _PERM],
    db: AsyncSession = Depends(get_db),
) -> ToolLinkOut:
    link = await credentials_service.set_active_link(
        db, cred_id=cred_id, link_id=link_id, user_id=membership.user_id
    )
    return ToolLinkOut.model_validate(link)
