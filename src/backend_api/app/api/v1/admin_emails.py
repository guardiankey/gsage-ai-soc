"""gSage AI — Admin: Email account endpoints.

Routes (prefix: /v1/orgs/{org_id}/admin):
    GET    /email-accounts                     List email accounts
    POST   /email-accounts                     Create email account
    GET    /email-accounts/{account_id}        Get account detail
    PATCH  /email-accounts/{account_id}        Update account
    DELETE /email-accounts/{account_id}        Delete account
    POST   /email-accounts/{account_id}/test   Test IMAP + SMTP connection
"""

from __future__ import annotations

import socket
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_db, require_org_admin
from src.backend_api.app.schemas.admin import (
    EmailAccountCreate,
    EmailAccountOut,
    EmailAccountUpdate,
    EmailConnectionTestResult,
)
from src.shared.models.email_account import GSageEmailAccount
from src.shared.models.user_organization import GSageUserOrganization

router = APIRouter()

_PASSWORD_PLACEHOLDER = "••••••••"


def _account_to_out(acc: GSageEmailAccount) -> EmailAccountOut:
    """Convert model to response schema — passwords are masked."""
    return EmailAccountOut(
        id=acc.id,
        org_id=acc.org_id,
        dept_id=acc.dept_id,
        display_name=acc.display_name,
        email=acc.email,
        is_active=acc.is_active,
        imap_host=acc.imap_host,
        imap_port=acc.imap_port,
        imap_use_tls=acc.imap_use_tls,
        imap_verify_ssl=acc.imap_verify_ssl,
        imap_username=acc.imap_username,
        imap_password_set=bool(acc._imap_password_encrypted),
        imap_folder=acc.imap_folder,
        imap_idle_supported=acc.imap_idle_supported,
        smtp_host=acc.smtp_host,
        smtp_port=acc.smtp_port,
        smtp_use_tls=acc.smtp_use_tls,
        smtp_verify_ssl=acc.smtp_verify_ssl,
        smtp_username=acc.smtp_username,
        smtp_password_set=bool(acc._smtp_password_encrypted),
        sender_name=acc.sender_name,
        subject_prefix=acc.subject_prefix,
        reply_footer=acc.reply_footer,
        unknown_sender_folder=acc.unknown_sender_folder,
        max_email_size_bytes=acc.max_email_size_bytes,
        polling_interval_seconds=acc.polling_interval_seconds,
        created_at=acc.created_at,
        updated_at=acc.updated_at,
    )


@router.get(
    "/email-accounts",
    response_model=list[EmailAccountOut],
    summary="List email accounts",
)
async def list_email_accounts(
    org_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> list[EmailAccountOut]:
    result = await db.execute(
        select(GSageEmailAccount)
        .where(GSageEmailAccount.org_id == org_id)
        .order_by(GSageEmailAccount.display_name)
    )
    return [_account_to_out(a) for a in result.scalars().all()]


@router.post(
    "/email-accounts",
    response_model=EmailAccountOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create email account",
)
async def create_email_account(
    org_id: uuid.UUID,
    payload: EmailAccountCreate,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> EmailAccountOut:
    acc = GSageEmailAccount(
        org_id=org_id,
        dept_id=payload.dept_id,
        display_name=payload.display_name,
        email=str(payload.email),
        is_active=payload.is_active,
        imap_host=payload.imap_host,
        imap_port=payload.imap_port,
        imap_use_tls=payload.imap_use_tls,
        imap_verify_ssl=payload.imap_verify_ssl,
        imap_username=payload.imap_username,
        imap_folder=payload.imap_folder,
        imap_idle_supported=payload.imap_idle_supported,
        smtp_host=payload.smtp_host,
        smtp_port=payload.smtp_port,
        smtp_use_tls=payload.smtp_use_tls,
        smtp_verify_ssl=payload.smtp_verify_ssl,
        smtp_username=payload.smtp_username,
        sender_name=payload.sender_name,
        subject_prefix=payload.subject_prefix,
        reply_footer=payload.reply_footer,
        unknown_sender_folder=payload.unknown_sender_folder,
        max_email_size_bytes=payload.max_email_size_bytes,
        polling_interval_seconds=payload.polling_interval_seconds,
    )
    acc.imap_password = payload.imap_password  # encrypts via property setter
    if payload.smtp_password:
        acc.smtp_password = payload.smtp_password  # encrypts via property setter
    # else: leave smtp_password_encrypted as None (unauthenticated relay)

    db.add(acc)
    await db.commit()
    await db.refresh(acc)
    return _account_to_out(acc)


@router.get(
    "/email-accounts/{account_id}",
    response_model=EmailAccountOut,
    summary="Get email account detail",
)
async def get_email_account(
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> EmailAccountOut:
    result = await db.execute(
        select(GSageEmailAccount).where(
            GSageEmailAccount.id == account_id,
            GSageEmailAccount.org_id == org_id,
        )
    )
    acc = result.scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Email account not found")
    return _account_to_out(acc)


@router.patch(
    "/email-accounts/{account_id}",
    response_model=EmailAccountOut,
    summary="Update email account",
)
async def update_email_account(
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    payload: EmailAccountUpdate,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> EmailAccountOut:
    result = await db.execute(
        select(GSageEmailAccount).where(
            GSageEmailAccount.id == account_id,
            GSageEmailAccount.org_id == org_id,
        )
    )
    acc = result.scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Email account not found")

    data = payload.model_dump(exclude_unset=True)

    # Handle encrypted fields separately (don't use setattr for them)
    imap_password = data.pop("imap_password", None)
    smtp_password = data.pop("smtp_password", None)

    for key, value in data.items():
        setattr(acc, key, value)

    if imap_password is not None:
        acc.imap_password = imap_password
    if smtp_password is not None:
        acc.smtp_password = smtp_password

    await db.commit()
    await db.refresh(acc)
    return _account_to_out(acc)


@router.delete(
    "/email-accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete email account",
)
async def delete_email_account(
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(GSageEmailAccount).where(
            GSageEmailAccount.id == account_id,
            GSageEmailAccount.org_id == org_id,
        )
    )
    acc = result.scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Email account not found")

    await db.delete(acc)
    await db.commit()


@router.post(
    "/email-accounts/{account_id}/test",
    response_model=EmailConnectionTestResult,
    summary="Test email account connectivity",
)
async def test_email_account(
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    _: Annotated[GSageUserOrganization, Depends(require_org_admin)],
    db: AsyncSession = Depends(get_db),
) -> EmailConnectionTestResult:
    """Attempt IMAP login and SMTP connection. Returns per-protocol results."""
    result = await db.execute(
        select(GSageEmailAccount).where(
            GSageEmailAccount.id == account_id,
            GSageEmailAccount.org_id == org_id,
        )
    )
    acc = result.scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Email account not found")

    imap_ok, imap_error = await _test_imap(acc)
    smtp_ok, smtp_error = await _test_smtp(acc)

    return EmailConnectionTestResult(
        imap_ok=imap_ok,
        smtp_ok=smtp_ok,
        imap_error=imap_error,
        smtp_error=smtp_error,
    )


async def _test_imap(acc: GSageEmailAccount) -> tuple[bool, str | None]:
    """Non-blocking IMAP login test with 5-second timeout."""
    import asyncio
    import imaplib

    try:
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _imap_login(acc)),
            timeout=5.0,
        )
        return True, None
    except asyncio.TimeoutError:
        return False, "Connection timed out after 5 seconds"
    except Exception as exc:
        return False, str(exc)


def _imap_login(acc: GSageEmailAccount) -> None:
    import imaplib
    import ssl
    if acc.imap_use_tls:
        ssl_ctx = ssl.create_default_context()
        if not acc.imap_verify_ssl:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        conn = imaplib.IMAP4_SSL(acc.imap_host, acc.imap_port, ssl_context=ssl_ctx)
    else:
        conn = imaplib.IMAP4(acc.imap_host, acc.imap_port)
    try:
        conn.login(acc.imap_username, acc.imap_password)
        conn.logout()
    finally:
        try:
            conn.shutdown()
        except Exception:
            pass


async def _test_smtp(acc: GSageEmailAccount) -> tuple[bool, str | None]:
    """Non-blocking SMTP login test with 5-second timeout."""
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _smtp_login(acc)),
            timeout=5.0,
        )
        return True, None
    except asyncio.TimeoutError:
        return False, "Connection timed out after 5 seconds"
    except Exception as exc:
        return False, str(exc)


def _smtp_login(acc: GSageEmailAccount) -> None:
    import smtplib
    import ssl
    ssl_ctx: ssl.SSLContext | None = None
    if acc.smtp_use_tls and not acc.smtp_verify_ssl:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    # Port 465 → implicit SSL; port 587 or other with TLS → STARTTLS; else → plain.
    if acc.smtp_use_tls and acc.smtp_port == 465:
        conn = smtplib.SMTP_SSL(acc.smtp_host, acc.smtp_port, timeout=5, context=ssl_ctx)
    elif acc.smtp_use_tls:
        conn = smtplib.SMTP(acc.smtp_host, acc.smtp_port, timeout=5)
        conn.starttls(context=ssl_ctx)
    else:
        conn = smtplib.SMTP(acc.smtp_host, acc.smtp_port, timeout=5)
    try:
        if acc.smtp_username:  # skip login for unauthenticated relay
            conn.login(acc.smtp_username, acc.smtp_password)
    finally:
        try:
            conn.quit()
        except Exception:
            pass
