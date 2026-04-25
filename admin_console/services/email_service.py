"""Admin Console — service functions for Email Accounts."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def list_email_accounts(
    db: AsyncSession,
    org_id: uuid.UUID,
) -> list[dict[str, Any]]:
    from src.shared.models.email_account import GSageEmailAccount  # noqa: PLC0415

    result = await db.execute(
        select(GSageEmailAccount)
        .where(GSageEmailAccount.org_id == org_id)
        .order_by(GSageEmailAccount.email)
    )
    return [_account_to_dict(a) for a in result.scalars().all()]


async def get_email_account(
    db: AsyncSession,
    account_id: uuid.UUID,
) -> Optional[dict[str, Any]]:
    from src.shared.models.email_account import GSageEmailAccount  # noqa: PLC0415

    result = await db.execute(
        select(GSageEmailAccount).where(GSageEmailAccount.id == account_id)
    )
    a = result.scalar_one_or_none()
    return _account_to_dict(a, reveal=True) if a else None


def _account_to_dict(a: Any, reveal: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": str(a.id),
        "org_id": str(a.org_id),
        "email": a.email,
        "display_name": a.display_name,
        "is_active": a.is_active,
        "imap_host": a.imap_host,
        "imap_port": a.imap_port,
        "imap_use_tls": a.imap_use_tls,
        "imap_username": a.imap_username,
        "smtp_host": a.smtp_host,
        "smtp_port": a.smtp_port,
        "smtp_use_tls": a.smtp_use_tls,
        "smtp_username": a.smtp_username,
        "sender_name": a.sender_name,
        "created_at": a.created_at.isoformat() if a.created_at else "",
    }
    if reveal:
        # Attempt decryption — masked if it fails
        try:
            from src.shared.security.encryption import get_encryption  # noqa: PLC0415

            enc = get_encryption()
            if a._imap_password_encrypted:
                d["imap_password"] = enc.decrypt(a._imap_password_encrypted)
            if a._smtp_password_encrypted:
                d["smtp_password"] = enc.decrypt(a._smtp_password_encrypted)
        except Exception:
            d["imap_password"] = "••••••"
            d["smtp_password"] = "••••••"
    return d
