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

    # When the provider is Entra OIDC the external_id is the AAD Object ID
    # (oid claim). Automatically populate teams_aad_object_id so Teams messages
    # can be resolved to this user without requiring manual admin configuration.
    if result.provider_name == "entra_oidc" and identity.external_id:
        if user.teams_aad_object_id != identity.external_id:
            user.teams_aad_object_id = identity.external_id
            logger.info(
                "user_sync: set teams_aad_object_id=%s for user '%s'",
                identity.external_id, identity.email,
            )

    # ── 2. Resolve target role from group_mapping ────────────────────────
    group_mapping: dict = provider_config.get("group_mapping") or {}
    default_role: str = provider_config.get("default_role") or "viewer"

    # Debug aid: log what the provider returned vs. what is configured.
    # The mapping is matched by GSageGroup.name (NOT slug); group_mapping keys
    # must equal the raw external identifiers returned by the provider — for
    # Entra OIDC those are the security-group Object IDs (UUIDs).
    matched_ext = [g for g in result.groups if g in group_mapping]
    unmatched_ext = [g for g in result.groups if g not in group_mapping]
    logger.info(
        "user_sync: provider='%s' user='%s' org='%s' — external_groups=%s "
        "matched=%s unmatched=%s configured_keys=%s",
        result.provider_name, identity.email, org.slug,
        list(result.groups), matched_ext, unmatched_ext,
        list(group_mapping.keys()),
    )

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

    logger.info(
        "user_sync: resolved role='%s' desired_local_groups=%s for user='%s' in org='%s'",
        resolved_role, sorted(desired_local_groups), identity.email, org.slug,
    )

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


# Department-scoped role priority (no "owner" at dept level).
_DEPT_ROLE_PRIORITY = {"admin": 3, "member": 2, "viewer": 1}
_DEPT_DEFAULT_ROLE = "member"


def _extract_dept_assignments(mapping_entry: dict) -> dict[str, str]:
    """Return a ``{dept_name: dept_role}`` map for a single group_mapping entry.

    Schema (all keys optional, additive):
      - ``department``  : str             — single department name.
      - ``departments`` : list[str]       — multiple department names.
      - ``dept_role``   : str | list[str] — role inside the dept(s).
          * If a single string, it applies to every department in the entry.
          * If a list shorter than the department list, the **last** value is
            repeated to pad it (so a 1-element list also works as "apply to all").
          * If longer than the department list, extra values are ignored.
          * Missing or unknown role falls back to ``member``.
    """
    dept_names: list[str] = []

    single = mapping_entry.get("department")
    if isinstance(single, str) and single.strip():
        dept_names.append(single.strip())

    multi = mapping_entry.get("departments")
    if isinstance(multi, list):
        for d in multi:
            if isinstance(d, str) and d.strip() and d.strip() not in dept_names:
                dept_names.append(d.strip())

    if not dept_names:
        return {}

    raw_roles = mapping_entry.get("dept_role")
    if isinstance(raw_roles, str):
        roles = [raw_roles]
    elif isinstance(raw_roles, list):
        roles = [r for r in raw_roles if isinstance(r, str)]
    else:
        roles = []

    # Pad shorter list by repeating the last value; if empty, default to "member".
    if not roles:
        roles = [_DEPT_DEFAULT_ROLE]
    while len(roles) < len(dept_names):
        roles.append(roles[-1])

    out: dict[str, str] = {}
    for name, role in zip(dept_names, roles):
        if role not in _DEPT_ROLE_PRIORITY:
            role = _DEPT_DEFAULT_ROLE
        out[name] = role
    return out


async def _sync_department_memberships(
    db: AsyncSession,
    user: GSageUser,
    org: GSageOrganization,
    group_mapping: dict,
    external_groups: list[str],
    provider_config: dict,
) -> None:
    """Sync department memberships from provider group_mapping.

    Each ``group_mapping`` entry may specify ``department`` (str) and/or
    ``departments`` (list[str]) plus an optional ``dept_role`` (str or
    list[str]).  See :func:`_extract_dept_assignments` for details.

    When the same department is assigned by multiple matched groups, the
    highest-priority dept role wins (admin > member > viewer).

    When ``auto_create_departments`` is True (default: False) missing
    departments are created automatically.

    Stale memberships (departments the user is currently in but no longer
    matched by any group) are **removed**.  This is a full sync so SSO
    remains the source of truth.

    If no department mapping is found at all the user is placed in the
    org's default department.
    """
    auto_create: bool = bool(provider_config.get("auto_create_departments", False))

    # ── 1. Build desired_depts: {dept_name: dept_role} from all matched groups ──
    desired_depts: dict[str, str] = {}
    for ext_group in external_groups:
        mapping_entry = group_mapping.get(ext_group)
        if not mapping_entry:
            continue
        for dept_name, role in _extract_dept_assignments(mapping_entry).items():
            current = desired_depts.get(dept_name)
            if current is None or _DEPT_ROLE_PRIORITY.get(role, 0) > _DEPT_ROLE_PRIORITY.get(current, 0):
                desired_depts[dept_name] = role

    if not desired_depts:
        # Fallback: ensure user is in the default department.
        # We do NOT prune other memberships here — admins may have manually
        # placed the user in additional departments.
        await dept_svc.ensure_user_in_default_department(db, user_id=user.id, org_id=org.id)
        return

    from src.shared.models.department import GSageDepartment

    # ── 2. Resolve / create departments and apply memberships ───────────────
    desired_dept_ids: set[uuid.UUID] = set()

    for dept_name, dept_role in desired_depts.items():
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
                    "user_sync: dept '%s' not found in org '%s' — "
                    "auto_create_departments=False, skip",
                    dept_name, org.slug,
                )
                continue
            dept = await dept_svc.create_department(
                db=db,
                org_id=org.id,
                name=dept_name,
            )
            logger.info(
                "user_sync: auto-created department '%s' in org '%s'",
                dept_name, org.slug,
            )

        desired_dept_ids.add(dept.id)

        existing = await dept_svc.get_membership(db, user_id=user.id, dept_id=dept.id)
        if existing is None:
            try:
                await dept_svc.add_member(
                    db=db,
                    dept_id=dept.id,
                    org_id=org.id,
                    user_id=user.id,
                    role=dept_role,
                )
                logger.debug(
                    "user_sync: added user '%s' to dept '%s' (role=%s)",
                    user.email, dept_name, dept_role,
                )
            except dept_svc.DepartmentConflict:
                pass
        else:
            changed = False
            if not existing.is_active:
                existing.is_active = True
                changed = True
            if existing.role != dept_role:
                logger.info(
                    "user_sync: updating dept role for '%s' in '%s': %s → %s",
                    user.email, dept_name, existing.role, dept_role,
                )
                existing.role = dept_role
                changed = True
            if changed:
                await db.flush()

    # ── 3. Remove stale memberships (full sync) ─────────────────────────────
    current = await dept_svc.get_user_departments(db, user_id=user.id, org_id=org.id)
    for membership in current:
        if membership.dept_id not in desired_dept_ids:
            try:
                await dept_svc.remove_member(
                    db=db,
                    dept_id=membership.dept_id,
                    org_id=org.id,
                    user_id=user.id,
                )
                logger.debug(
                    "user_sync: removed user '%s' from stale dept_id=%s",
                    user.email, membership.dept_id,
                )
            except dept_svc.DepartmentNotFound:
                pass

    await db.flush()
