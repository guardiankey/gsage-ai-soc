"""Admin Console — service functions for Users."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def list_users(
    db: AsyncSession,
    org_id: Optional[uuid.UUID] = None,
    dept_id: Optional[uuid.UUID] = None,
    org_name: Optional[str] = None,  # kept for backward compatibility — ignored
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return users, optionally filtered to an org/department membership."""
    from src.shared.models.user import GSageUser  # noqa: PLC0415
    from src.shared.models.user_organization import GSageUserOrganization  # noqa: PLC0415

    if org_id:
        # JOIN to resolve the org name from the DB for each user
        from src.shared.models.organization import GSageOrganization  # noqa: PLC0415

        stmt = (
            select(GSageUser, GSageOrganization.name)
            .join(GSageUserOrganization, GSageUserOrganization.user_id == GSageUser.id)
            .join(GSageOrganization, GSageOrganization.id == GSageUserOrganization.org_id)
            .where(GSageUserOrganization.org_id == org_id)
        )
        if dept_id:
            from src.shared.models.user_department import GSageUserDepartment  # noqa: PLC0415

            stmt = stmt.join(
                GSageUserDepartment,
                GSageUserDepartment.user_id == GSageUser.id,
            ).where(GSageUserDepartment.dept_id == dept_id)

        stmt = stmt.order_by(GSageUser.email).limit(limit)
        result = await db.execute(stmt)
        return [
            {**_user_to_dict(u), "org_name": fetched_name or ""}
            for u, fetched_name in result.all()
        ]

    # No org filter — no org name available
    stmt = select(GSageUser).distinct()
    if dept_id:
        from src.shared.models.user_department import GSageUserDepartment  # noqa: PLC0415

        stmt = stmt.join(
            GSageUserDepartment,
            GSageUserDepartment.user_id == GSageUser.id,
        ).where(GSageUserDepartment.dept_id == dept_id)

    stmt = stmt.order_by(GSageUser.email).limit(limit)
    result = await db.execute(stmt)
    return [_user_to_dict(u) for u in result.scalars().all()]


async def get_user(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> Optional[dict[str, Any]]:
    from src.shared.models.user import GSageUser  # noqa: PLC0415

    result = await db.execute(
        select(GSageUser).where(GSageUser.id == user_id)
    )
    u = result.scalar_one_or_none()
    return _user_to_dict(u) if u else None


async def get_user_by_email(
    db: AsyncSession,
    email: str,
) -> Optional[dict[str, Any]]:
    """Return user by email (case-insensitive) or None."""
    from src.shared.models.user import GSageUser  # noqa: PLC0415

    result = await db.execute(
        select(GSageUser).where(GSageUser.email == email.strip().lower())
    )
    u = result.scalar_one_or_none()
    return _user_to_dict(u) if u else None


async def link_user_to_org(
    db: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    role: str = "member",
) -> Optional[dict[str, Any]]:
    """Add an existing user to an organization (create membership row)."""
    from src.shared.models.user_organization import GSageUserOrganization  # noqa: PLC0415

    membership = GSageUserOrganization(
        user_id=user_id,
        org_id=org_id,
        role=role,
        is_active=True,
    )
    db.add(membership)
    await db.commit()
    return await get_user(db, user_id)


async def create_user(
    db: AsyncSession,
    email: str,
    full_name: str,
    password: str,
    org_id: Optional[uuid.UUID] = None,
    role: str = "member",
) -> dict[str, Any]:
    from src.shared.models.user import GSageUser  # noqa: PLC0415
    from src.shared.models.user_organization import GSageUserOrganization  # noqa: PLC0415
    from src.shared.security.auth import hash_password  # noqa: PLC0415

    user = GSageUser(
        email=email.strip().lower(),
        full_name=full_name.strip(),
        password_hash=hash_password(password),
        is_active=True,
        auth_provider="local",
    )
    db.add(user)
    await db.flush()

    if org_id:
        membership = GSageUserOrganization(
            user_id=user.id,
            org_id=org_id,
            role=role,
            is_active=True,
        )
        db.add(membership)

    await db.commit()
    await db.refresh(user)
    return _user_to_dict(user)


async def update_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    **fields: Any,
) -> Optional[dict[str, Any]]:
    from src.shared.models.user import GSageUser  # noqa: PLC0415

    if "password" in fields:
        from src.shared.security.auth import hash_password  # noqa: PLC0415

        fields["password_hash"] = hash_password(fields.pop("password"))

    await db.execute(
        update(GSageUser).where(GSageUser.id == user_id).values(**fields)
    )
    await db.commit()
    return await get_user(db, user_id)


async def toggle_user_active(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> Optional[dict[str, Any]]:
    from src.shared.models.user import GSageUser  # noqa: PLC0415

    result = await db.execute(
        select(GSageUser).where(GSageUser.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        return None
    user.is_active = not user.is_active
    await db.commit()
    await db.refresh(user)
    return _user_to_dict(user)


async def reset_password(
    db: AsyncSession,
    user_id: uuid.UUID,
    new_password: str,
) -> bool:
    from src.shared.models.user import GSageUser  # noqa: PLC0415
    from src.shared.security.auth import hash_password  # noqa: PLC0415

    await db.execute(
        update(GSageUser)
        .where(GSageUser.id == user_id)
        .values(password_hash=hash_password(new_password))
    )
    await db.commit()
    return True


async def reset_otp(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> bool:
    """Clear OTP secret and disable 2FA for a user."""
    from src.shared.models.user import GSageUser  # noqa: PLC0415

    await db.execute(
        update(GSageUser)
        .where(GSageUser.id == user_id)
        .values(
            otp_enabled=False,
            otp_confirmed_at=None,
            _otp_secret_encrypted=None,
            _otp_backup_codes_encrypted=None,
        )
    )
    await db.commit()
    return True


async def get_user_memberships(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> list[dict[str, Any]]:
    from src.shared.models.user_organization import GSageUserOrganization  # noqa: PLC0415
    from src.shared.models.organization import GSageOrganization  # noqa: PLC0415

    stmt = (
        select(GSageUserOrganization, GSageOrganization)
        .join(
            GSageOrganization,
            GSageOrganization.id == GSageUserOrganization.org_id,
        )
        .where(GSageUserOrganization.user_id == user_id)
    )
    result = await db.execute(stmt)
    rows = result.all()
    return [
        {
            "org_id": str(m.org_id),
            "org_name": o.name,
            "role": m.role,
            "is_active": m.is_active,
        }
        for m, o in rows
    ]


def _user_to_dict(user: Any) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "is_active": user.is_active,
        "is_superuser": getattr(user, "is_superuser", False),
        "auth_provider": user.auth_provider,
        "otp_enabled": user.otp_enabled,
        "created_at": user.created_at.isoformat() if user.created_at else "",
        "org_name": "",
    }
