"""gSage AI — User upsert and group sync for external providers.

Called after a successful external authentication to ensure the local
GSageUser and GSageUserOrganization records are up-to-date.

Flow
----
1. Find user by external_id (preferred stable key) or email (fallback).
2. If not found → create GSageUser with ``password_hash=None``.
3. Update mutable profile fields (full_name, email) if they changed.
4. Ensure a GSageUserOrganization row exists for this org.
5. Resolve role from group_mapping (highest-priority match wins).
6. Sync GSageGroup memberships:
   a. Map external group identifiers → local group names via group_mapping.
   b. Optionally auto-create missing GSageGroup rows.
   c. Remove stale group memberships no longer present in the external result.
7. Return (GSageUser, GSageUserOrganization).

For the built-in LocalAuthProvider this module is not called — the user
already exists and group memberships are managed via the admin UI.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.shared.auth.base import AuthResult
from src.shared.models.group import GSageGroup
from src.shared.models.organization import GSageOrganization
from src.shared.models.user import GSageUser, gsage_user_groups
from src.shared.models.user_organization import GSageUserOrganization
from src.shared.services import department_service as dept_svc

logger = logging.getLogger(__name__)


async def upsert_external_user(
    db: AsyncSession,
    org: GSageOrganization,
    result: AuthResult,
    provider_config: dict,
) -> tuple[GSageUser, GSageUserOrganization]:
    """Upsert a user from an external auth provider and sync their groups.

    Parameters
    ----------
    db:
        Open AsyncSession.  The function flushes but does NOT commit —
        the caller (login route) owns the transaction.
    org:
        The target organisation.
    result:
        Successful AuthResult from the provider chain.
    provider_config:
        Merged provider config for this org.  Used for ``group_mapping``,
        ``default_role``, and ``auto_create_groups``.

    Returns
    -------
    (GSageUser, GSageUserOrganization)
    """
    assert result.success and result.identity, "upsert_external_user called on failed result"
    identity = result.identity

    # ── 1. Find or create GSageUser ───────────────────────────────────
    user: Optional[GSageUser] = None

    if identity.external_id:
        res = await db.execute(
            select(GSageUser).where(
                GSageUser.external_id == identity.external_id,
            )
        )
        user = res.scalar_one_or_none()

    if user is None:
        # Fallback: match by email
        res = await db.execute(
            select(GSageUser).where(GSageUser.email == identity.email)
        )
        user = res.scalar_one_or_none()

    if user is None:
        # Auto-provision a new user
        user = GSageUser(
            email=identity.email,
            full_name=identity.full_name,
            password_hash=None,          # external users have no local password
            auth_provider=result.provider_name,
            external_id=identity.external_id,
            is_active=True,
        )
        db.add(user)
        await db.flush()
        logger.info(
            "user_sync: auto-provisioned user '%s' from provider '%s'",
            identity.email, result.provider_name,
        )
    else:
        # Update mutable fields that may have changed in the external directory
        user.full_name = identity.full_name
        if identity.external_id and user.external_id != identity.external_id:
            user.external_id = identity.external_id
        user.auth_provider = result.provider_name
        if not user.is_active:
            user.is_active = True
            logger.info(
                "user_sync: re-activated user '%s'", identity.email
            )

    # ── 2. Resolve target role from group_mapping ────────────────────────
    group_mapping: dict = provider_config.get("group_mapping") or {}
    default_role: str = provider_config.get("default_role") or "viewer"

    # Role priority order (highest → lowest)
    _ROLE_PRIORITY = {"owner": 4, "admin": 3, "member": 2, "viewer": 1}
    resolved_role = default_role

    # The external groups in result.groups are the raw identifiers returned
    # by the provider (e.g. full LDAP DNs or short CN names).
    # group_mapping keys should match those identifiers exactly.
    for ext_group in result.groups:
        mapping_entry = group_mapping.get(ext_group)
        if not mapping_entry:
            continue
        candidate_role: str = mapping_entry.get("role", default_role)
        if _ROLE_PRIORITY.get(candidate_role, 0) > _ROLE_PRIORITY.get(resolved_role, 0):
            resolved_role = candidate_role

    # ── 3. Upsert GSageUserOrganization ───────────────────────────────
    mem_res = await db.execute(
        select(GSageUserOrganization).where(
            GSageUserOrganization.user_id == user.id,
            GSageUserOrganization.org_id == org.id,
        )
    )
    membership: Optional[GSageUserOrganization] = mem_res.scalar_one_or_none()

    if membership is None:
        membership = GSageUserOrganization(
            user_id=user.id,
            org_id=org.id,
            role=resolved_role,
            is_active=True,
        )
        db.add(membership)
        logger.info(
            "user_sync: added '%s' to org '%s' with role '%s'",
            identity.email, org.slug, resolved_role,
        )
    else:
        if membership.role != resolved_role:
            logger.info(
                "user_sync: updating role for '%s' in '%s': %s → %s",
                identity.email, org.slug, membership.role, resolved_role,
            )
            membership.role = resolved_role
        if not membership.is_active:
            membership.is_active = True

    await db.flush()

    # ── 4. Sync GSageGroup memberships ────────────────────────────────
    auto_create: bool = bool(provider_config.get("auto_create_groups", True))

    # Collect all local group names the user should belong to after this login
    desired_local_groups: set[str] = set()
    for ext_group in result.groups:
        mapping_entry = group_mapping.get(ext_group)
        if not mapping_entry:
            continue
        for local_group_name in mapping_entry.get("groups") or []:
            desired_local_groups.add(local_group_name)

    if desired_local_groups or result.groups:
        await _sync_group_memberships(
            db=db,
            user=user,
            org=org,
            desired_group_names=desired_local_groups,
            auto_create=auto_create,
        )

    # ── 5. Sync department memberships ───────────────────────────────────
    await _sync_department_memberships(
        db=db,
        user=user,
        org=org,
        group_mapping=group_mapping,
        external_groups=result.groups,
        provider_config=provider_config,
    )

    return user, membership


async def _sync_group_memberships(
    db: AsyncSession,
    user: GSageUser,
    org: GSageOrganization,
    desired_group_names: set[str],
    auto_create: bool,
) -> None:
    """Synchronise ``user.groups`` within *org* to match *desired_group_names*.

    Groups in other orgs are never touched.
    """
    # Load user with current groups (org-scoped)
    res = await db.execute(
        select(GSageUser)
        .where(GSageUser.id == user.id)
        .options(selectinload(GSageUser.groups))
    )
    user_with_groups = res.scalar_one()

    # Current org group names the user belongs to
    current_org_groups = {
        g for g in user_with_groups.groups if g.org_id == org.id
    }
    current_names = {g.name for g in current_org_groups}

    # ── Add missing groups ────────────────────────────────────────────────
    for gname in desired_group_names - current_names:
        # Find or create the GSageGroup
        gres = await db.execute(
            select(GSageGroup).where(
                GSageGroup.org_id == org.id,
                GSageGroup.name == gname,
            )
        )
        group = gres.scalar_one_or_none()

        if group is None:
            if not auto_create:
                logger.debug(
                    "user_sync: group '%s' not found in org '%s' and auto_create=False — skip",
                    gname, org.slug,
                )
                continue
            group = GSageGroup(
                org_id=org.id,
                name=gname,
                description=f"Auto-created by auth sync",
            )
            db.add(group)
            await db.flush()
            logger.info(
                "user_sync: auto-created group '%s' in org '%s'", gname, org.slug
            )

        user_with_groups.groups.append(group)
        logger.debug(
            "user_sync: added user '%s' to group '%s'", user.email, gname
        )

    # ── Remove stale group memberships ───────────────────────────────────
    for group in list(current_org_groups):
        if group.name not in desired_group_names:
            user_with_groups.groups.remove(group)
            logger.debug(
                "user_sync: removed user '%s' from stale group '%s'",
                user.email, group.name,
            )

    await db.flush()


async def _sync_department_memberships(
    db: AsyncSession,
    user: GSageUser,
    org: GSageOrganization,
    group_mapping: dict,
    external_groups: list[str],
    provider_config: dict,
) -> None:
    """Sync department memberships from provider group_mapping.

    Each group_mapping entry may specify a ``department`` key with the
    department name to assign the user to.  When ``auto_create_departments``
    is True (default: False) missing departments are created automatically.

    If no department mapping is found at all the user is placed in the
    org's default department.
    """
    auto_create: bool = bool(provider_config.get("auto_create_departments", False))

    # Collect desired department names from mapping
    desired_dept_names: set[str] = set()
    for ext_group in external_groups:
        mapping_entry = group_mapping.get(ext_group)
        if not mapping_entry:
            continue
        dept_name: Optional[str] = mapping_entry.get("department")
        if dept_name:
            desired_dept_names.add(dept_name)

    if not desired_dept_names:
        # Fallback: ensure user is in the default department
        await dept_svc.ensure_user_in_default_department(db, user_id=user.id, org_id=org.id)
        return

    from src.shared.models.department import GSageDepartment

    for dept_name in desired_dept_names:
        # Find department by name within org
        dept_res = await db.execute(
            select(GSageDepartment).where(
                GSageDepartment.org_id == org.id,
                GSageDepartment.name == dept_name,
            )
        )
        dept = dept_res.scalar_one_or_none()

        if dept is None:
            if not auto_create:
                logger.debug(
                    "user_sync: dept '%s' not found in org '%s' — auto_create_departments=False, skip",
                    dept_name, org.slug,
                )
                continue
            dept = await dept_svc.create_department(
                db=db,
                org_id=org.id,
                name=dept_name,
            )
            logger.info(
                "user_sync: auto-created department '%s' in org '%s'", dept_name, org.slug
            )

        try:
            await dept_svc.add_member(
                db=db,
                dept_id=dept.id,
                org_id=org.id,
                user_id=user.id,
            )
            logger.debug("user_sync: added user '%s' to department '%s'", user.email, dept_name)
        except dept_svc.DepartmentConflict:
            # Already a member — that's fine
            pass

    await db.flush()
