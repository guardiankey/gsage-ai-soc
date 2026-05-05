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

import logging
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

logger = logging.getLogger(__name__)

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
        auth_method=acc.auth_method,
        oauth_tenant_id=acc.oauth_tenant_id,
        oauth_client_id=acc.oauth_client_id,
        oauth_token_endpoint=acc.oauth_token_endpoint,
        oauth_scope=acc.oauth_scope,
        oauth_client_secret_set=bool(acc._oauth_client_secret_encrypted),
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
        auth_method=payload.auth_method,
        oauth_tenant_id=payload.oauth_tenant_id,
        oauth_client_id=payload.oauth_client_id,
        oauth_token_endpoint=payload.oauth_token_endpoint,
        oauth_scope=payload.oauth_scope,
    )
    if payload.imap_password:
        acc.imap_password = payload.imap_password  # encrypts via property setter
    if payload.smtp_password:
        acc.smtp_password = payload.smtp_password  # encrypts via property setter
    if payload.oauth_client_secret:
        acc.oauth_client_secret = payload.oauth_client_secret  # encrypts via property setter
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
    oauth_client_secret = data.pop("oauth_client_secret", None)

    for key, value in data.items():
        setattr(acc, key, value)

    if imap_password is not None:
        acc.imap_password = imap_password
    if smtp_password is not None:
        acc.smtp_password = smtp_password
    if oauth_client_secret is not None:
        acc.oauth_client_secret = oauth_client_secret

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

    logger.info(
        "email-account test: starting — account=%s id=%s auth_method=%s "
        "imap=%s:%d (tls=%s verify=%s user=%s) "
        "smtp=%s:%d (tls=%s verify=%s user=%s)",
        acc.email, acc.id, acc.auth_method,
        acc.imap_host, acc.imap_port, acc.imap_use_tls, acc.imap_verify_ssl,
        acc.imap_username,
        acc.smtp_host, acc.smtp_port, acc.smtp_use_tls, acc.smtp_verify_ssl,
        acc.smtp_username,
    )

    imap_ok, imap_error = await _test_imap(acc)
    smtp_ok, smtp_error = await _test_smtp(acc)

    logger.info(
        "email-account test: done — account=%s imap_ok=%s imap_error=%s "
        "smtp_ok=%s smtp_error=%s",
        acc.email, imap_ok, imap_error, smtp_ok, smtp_error,
    )

    return EmailConnectionTestResult(
        imap_ok=imap_ok,
        smtp_ok=smtp_ok,
        imap_error=imap_error,
        smtp_error=smtp_error,
    )


async def _test_imap(acc: GSageEmailAccount) -> tuple[bool, str | None]:
    """Non-blocking IMAP login test with 5-second timeout."""
    import asyncio

    try:
        # OAuth2 path is async (httpx for token); basic uses thread pool.
        if acc.auth_method == "oauth2":
            from src.shared.services.oauth_token import get_access_token

            logger.info(
                "email-account test [IMAP]: acquiring OAuth2 token — account=%s",
                acc.email,
            )
            token = await asyncio.wait_for(get_access_token(acc), timeout=10.0)
            logger.info(
                "email-account test [IMAP]: token acquired — account=%s token_len=%d",
                acc.email, len(token),
            )
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _imap_login_oauth2(acc, token)),
                timeout=5.0,
            )
        else:
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _imap_login(acc)),
                timeout=5.0,
            )
        logger.info("email-account test [IMAP]: success — account=%s", acc.email)
        return True, None
    except asyncio.TimeoutError:
        logger.warning(
            "email-account test [IMAP]: timeout after 5s — account=%s host=%s:%d",
            acc.email, acc.imap_host, acc.imap_port,
        )
        return False, "Connection timed out after 5 seconds"
    except Exception as exc:
        logger.error(
            "email-account test [IMAP]: failure — account=%s host=%s:%d "
            "auth_method=%s username=%s error=%s",
            acc.email, acc.imap_host, acc.imap_port, acc.auth_method,
            acc.imap_username, exc,
            exc_info=True,
        )
        return False, str(exc)


def _imap_login(acc: GSageEmailAccount) -> None:
    import imaplib
    import ssl
    # IMPORTANT: pass timeout=5 to bound socket.connect().  Without it the OS
    # default TCP timeout (minutes) leaves the worker thread alive after
    # asyncio.wait_for cancels the Future, which in turn keeps anyio busy-
    # looping in _deliver_cancellation (high CPU with no requests).
    if acc.imap_use_tls:
        ssl_ctx = ssl.create_default_context()
        if not acc.imap_verify_ssl:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        conn = imaplib.IMAP4_SSL(
            acc.imap_host, acc.imap_port, ssl_context=ssl_ctx, timeout=5
        )
    else:
        conn = imaplib.IMAP4(acc.imap_host, acc.imap_port, timeout=5)
    try:
        conn.login(acc.imap_username, acc.imap_password)
        conn.logout()
    finally:
        try:
            conn.shutdown()
        except Exception:
            pass


def _imap_login_oauth2(acc: GSageEmailAccount, token: str) -> None:
    """IMAP XOAUTH2 login (Microsoft 365 client-credentials)."""
    import imaplib
    import ssl
    from src.shared.services.oauth_token import build_xoauth2_string

    if acc.imap_use_tls:
        ssl_ctx = ssl.create_default_context()
        if not acc.imap_verify_ssl:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        conn = imaplib.IMAP4_SSL(
            acc.imap_host, acc.imap_port, ssl_context=ssl_ctx, timeout=5
        )
    else:
        conn = imaplib.IMAP4(acc.imap_host, acc.imap_port, timeout=5)
    try:
        try:
            caps = " ".join(
                c.decode(errors="replace") if isinstance(c, bytes) else str(c)
                for c in (conn.capabilities or ())
            )
            logger.info(
                "email-account test [IMAP]: connected — account=%s caps=%s",
                acc.email, caps,
            )
        except Exception:
            pass
        username = acc.imap_username or acc.email
        sasl = build_xoauth2_string(username, token)
        logger.info(
            "email-account test [IMAP]: sending XOAUTH2 — account=%s username=%s "
            "token_len=%d",
            acc.email, username, len(token),
        )
        try:
            typ, data = conn.authenticate("XOAUTH2", lambda _challenge: sasl.encode("utf-8"))
        except imaplib.IMAP4.error as exc:
            decoded = str(exc)
            logger.error(
                "email-account test [IMAP]: XOAUTH2 rejected — account=%s "
                "username=%s error=%s. Common causes: "
                "(1) imap_username must be the mailbox UPN; "
                "(2) the AAD app needs Office 365 Exchange Online > "
                "IMAP.AccessAsApp (application) with admin consent; "
                "(3) the service principal needs FullAccess on the mailbox "
                "(Add-MailboxPermission via Exchange Online PowerShell).",
                acc.email, username, decoded,
            )
            raise
        logger.info(
            "email-account test [IMAP]: XOAUTH2 accepted — account=%s typ=%s data=%r",
            acc.email, typ, data,
        )
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
        if acc.auth_method == "oauth2":
            from src.shared.services.oauth_token import get_access_token

            logger.info(
                "email-account test [SMTP]: acquiring OAuth2 token — account=%s",
                acc.email,
            )
            token = await asyncio.wait_for(get_access_token(acc), timeout=10.0)
            logger.info(
                "email-account test [SMTP]: token acquired — account=%s token_len=%d",
                acc.email, len(token),
            )
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _smtp_login_oauth2(acc, token)),
                timeout=5.0,
            )
        else:
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _smtp_login(acc)),
                timeout=5.0,
            )
        logger.info("email-account test [SMTP]: success — account=%s", acc.email)
        return True, None
    except asyncio.TimeoutError:
        logger.warning(
            "email-account test [SMTP]: timeout after 5s — account=%s host=%s:%d",
            acc.email, acc.smtp_host, acc.smtp_port,
        )
        return False, "Connection timed out after 5 seconds"
    except Exception as exc:
        logger.error(
            "email-account test [SMTP]: failure — account=%s host=%s:%d "
            "auth_method=%s username=%s error=%s",
            acc.email, acc.smtp_host, acc.smtp_port, acc.auth_method,
            acc.smtp_username, exc,
            exc_info=True,
        )
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


def _smtp_login_oauth2(acc: GSageEmailAccount, token: str) -> None:
    """SMTP XOAUTH2 login (Microsoft 365 client-credentials)."""
    import base64
    import smtplib
    import ssl
    from src.shared.services.oauth_token import build_xoauth2_string

    ssl_ctx: ssl.SSLContext | None = None
    if acc.smtp_use_tls and not acc.smtp_verify_ssl:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    if acc.smtp_use_tls and acc.smtp_port == 465:
        conn = smtplib.SMTP_SSL(acc.smtp_host, acc.smtp_port, timeout=5, context=ssl_ctx)
        logger.info(
            "email-account test [SMTP]: implicit-TLS connected — account=%s host=%s:%d",
            acc.email, acc.smtp_host, acc.smtp_port,
        )
    elif acc.smtp_use_tls:
        conn = smtplib.SMTP(acc.smtp_host, acc.smtp_port, timeout=5)
        conn.ehlo()
        conn.starttls(context=ssl_ctx)
        conn.ehlo()
        logger.info(
            "email-account test [SMTP]: STARTTLS negotiated — account=%s host=%s:%d "
            "features=%s",
            acc.email, acc.smtp_host, acc.smtp_port,
            list((conn.esmtp_features or {}).keys()),
        )
    else:
        conn = smtplib.SMTP(acc.smtp_host, acc.smtp_port, timeout=5)
        conn.ehlo()
        logger.info(
            "email-account test [SMTP]: plain connected — account=%s features=%s",
            acc.email, list((conn.esmtp_features or {}).keys()),
        )
    try:
        auth_features = (conn.esmtp_features or {}).get("auth", "")
        logger.info(
            "email-account test [SMTP]: auth mechanisms advertised — account=%s auth=%r",
            acc.email, auth_features,
        )
        if "XOAUTH2" not in (auth_features or "").upper():
            logger.warning(
                "email-account test [SMTP]: server does not advertise XOAUTH2 — "
                "account=%s. For Office 365 verify SMTP AUTH is enabled on the "
                "mailbox (Set-CASMailbox -SmtpClientAuthenticationDisabled \\$false) "
                "and tenant-wide (Set-TransportConfig "
                "-SmtpClientAuthenticationDisabled \\$false).",
                acc.email,
            )
        username = acc.smtp_username or acc.email
        sasl = build_xoauth2_string(username, token)
        sasl_b64 = base64.b64encode(sasl.encode("utf-8")).decode("ascii")
        logger.info(
            "email-account test [SMTP]: sending AUTH XOAUTH2 — account=%s "
            "username=%s token_len=%d",
            acc.email, username, len(token),
        )
        code, msg = conn.docmd("AUTH", f"XOAUTH2 {sasl_b64}")
        msg_decoded = (
            msg.decode(errors="replace") if isinstance(msg, (bytes, bytearray))
            else str(msg)
        )
        # 334 is a continuation challenge — server sent base64-encoded JSON
        # describing the failure. Decode and surface it.
        if code == 334:
            try:
                challenge_json = base64.b64decode(msg_decoded).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                challenge_json = msg_decoded
            # ACK the continuation so the server returns final status.
            final_code, final_msg = conn.docmd("")
            final_decoded = (
                final_msg.decode(errors="replace")
                if isinstance(final_msg, (bytes, bytearray)) else str(final_msg)
            )
            logger.error(
                "email-account test [SMTP]: XOAUTH2 challenge=%s final=%d %s — "
                "account=%s. Common causes: "
                "(1) smtp_username must be the mailbox UPN; "
                "(2) the AAD app needs Office 365 Exchange Online > "
                "SMTP.SendAsApp (application) with admin consent; "
                "(3) Add-MailboxPermission grants the SP send-as on the mailbox; "
                "(4) tenant SMTP AUTH must not be disabled.",
                challenge_json, final_code, final_decoded, acc.email,
            )
            raise RuntimeError(
                f"SMTP XOAUTH2 failed: {final_code} {final_decoded} "
                f"(challenge={challenge_json})"
            )
        if code < 200 or code >= 300:
            logger.error(
                "email-account test [SMTP]: XOAUTH2 rejected — account=%s "
                "code=%d msg=%s",
                acc.email, code, msg_decoded,
            )
            raise RuntimeError(f"SMTP XOAUTH2 failed: {code} {msg_decoded!r}")
        logger.info(
            "email-account test [SMTP]: XOAUTH2 accepted — account=%s code=%d msg=%s",
            acc.email, code, msg_decoded,
        )
    finally:
        try:
            conn.quit()
        except Exception:
            pass
