"""gSage AI — Email Worker (IMAP/SMTP) entry point (Phase 7).

Architecture
------------
One asyncio task is spawned per active GSageEmailAccount.  Each task:

  1. Opens an IMAP connection via IMAPClientWrapper.
  2. Enters IDLE mode (or polling fallback).
  3. When a new email arrives:
       a. Parse raw bytes → ParsedEmail (parser.py).
       b. Reject oversized or malformed emails silently.
       c. Persist a GSageEmailMessage record (status=PENDING) in PostgreSQL.
       d. Dispatch a ``process_email_inbound`` Celery task.

The Celery task (tasks/email.py) handles all downstream logic:
sender resolution, rate limiting, thread management, agent dispatch, SMTP reply.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.logging import configure_logging

# Configure structured JSON logging before anything else
configure_logging("email_worker")

logger = logging.getLogger(__name__)


class EmailWorker:
    """Async IMAP worker — one connection per active email account."""

    def __init__(self) -> None:
        self._running = False
        self._tasks: list[asyncio.Task] = []

    # ── Public interface ──────────────────────────────────────────────────

    def start(self) -> None:
        """Entry point (blocking). Runs the asyncio event loop."""
        self._running = True
        logger.info("EmailWorker: starting...")
        asyncio.run(self._run())

    def stop(self) -> None:
        """Signal the worker to stop (safe to call from signal handler)."""
        logger.info("EmailWorker: stop requested")
        self._running = False
        for task in self._tasks:
            task.cancel()

    # ── Core asyncio loop ─────────────────────────────────────────────────

    async def _run(self) -> None:
        from src.shared.config.settings import get_settings
        from src.shared.models.email_account import GSageEmailAccount

        settings = get_settings()
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        logger.info("EmailWorker: loading active email accounts...")
        async with Session() as session:
            result = await session.execute(
                select(GSageEmailAccount).where(GSageEmailAccount.is_active == True)  # noqa: E712
            )
            accounts = result.scalars().all()

        if not accounts:
            logger.info(
                "EmailWorker: no active email accounts found — "
                "waiting for accounts to be configured (polling every 60s)..."
            )
            while self._running:
                await asyncio.sleep(60)
                async with Session() as session:
                    result = await session.execute(
                        select(GSageEmailAccount).where(
                            GSageEmailAccount.is_active == True  # noqa: E712
                        )
                    )
                    accounts = result.scalars().all()
                if accounts:
                    logger.info(
                        "EmailWorker: %d account(s) now available — starting IMAP tasks",
                        len(accounts),
                    )
                    break
            if not accounts:
                # _running was set to False externally (shutdown signal)
                await engine.dispose()
                return

        logger.info("EmailWorker: %d active account(s) found", len(accounts))

        self._tasks = [
            asyncio.create_task(
                self._account_loop(account, Session),
                name=f"imap-{account.email}",
            )
            for account in accounts
        ]

        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            await engine.dispose()
            logger.info("EmailWorker: stopped")

    # ── Per-account loop ──────────────────────────────────────────────────

    async def _account_loop(
        self,
        account,
        Session: async_sessionmaker,
    ) -> None:
        """Monitor one email account, reconnecting indefinitely.

        The account config is re-read from the DB on every (re)connect cycle so
        that changes made via the admin API (e.g. imap_verify_ssl, credentials)
        take effect without restarting the worker.
        """
        from src.shared.models.email_account import GSageEmailAccount
        from src.email_worker.imap_client import IMAPClientWrapper

        account_id = account.id
        back_off = 2
        while self._running:
            # Re-load account from DB to pick up any config changes.
            try:
                async with Session() as _s:
                    account = await _s.get(GSageEmailAccount, account_id)
                if account is None:
                    logger.warning(
                        "EmailWorker._account_loop: account %s no longer exists — stopping loop",
                        account_id,
                    )
                    return
                if not account.is_active:
                    logger.info(
                        "EmailWorker._account_loop: account %s deactivated — stopping loop",
                        account.email,
                    )
                    return
            except Exception as reload_exc:
                logger.warning(
                    "EmailWorker._account_loop: failed to reload account %s — using cached config: %s",
                    account_id, reload_exc,
                )

            client = IMAPClientWrapper(account)
            try:
                await client.connect()
                back_off = 2  # reset on successful connect

                async def on_email(
                    raw_bytes: bytes,
                    acct_id: uuid.UUID,
                    uid: str,
                    _client=client,
                    _account=account,  # capture freshly loaded account
                ) -> None:
                    await self._on_new_email(
                        raw_bytes, acct_id, uid, _client, _account, Session
                    )

                if account.imap_idle_supported:
                    await client.idle_loop(on_email)
                else:
                    await client.poll_loop(on_email, account.polling_interval_seconds)

            except asyncio.CancelledError:
                await client.disconnect()
                return

            except Exception as exc:
                logger.error(
                    "EmailWorker._account_loop: error — account=%s error=%s; "
                    "reconnecting in %ds",
                    account.email,
                    exc,
                    back_off,
                )
                await client.disconnect()
                await asyncio.sleep(back_off)
                back_off = min(back_off * 2, 60)

    # ── Email ingestion ───────────────────────────────────────────────────

    async def _on_new_email(
        self,
        raw_bytes: bytes,
        account_id: uuid.UUID,
        uid: str,
        imap_client,
        account,
        Session: async_sessionmaker,
    ) -> None:
        """Parse → check sender → persist → dispatch Celery task for a single raw email."""
        from src.email_worker.parser import parse_raw_email
        from src.email_worker.resolver import resolve_sender
        from src.shared.config.settings import get_settings
        from src.shared.models.email_message import (
            GSageEmailMessage,
            GSageEmailDirection,
            GSageEmailStatus,
        )
        from src.backend_api.app.tasks.email import process_email_inbound

        settings = get_settings()

        parsed = parse_raw_email(raw_bytes, max_size_bytes=account.max_email_size_bytes)
        if parsed is None:
            logger.debug(
                "EmailWorker._on_new_email: email discarded (parse failed) — account=%s",
                account.email,
            )
            return

        logger.info(
            "EmailWorker._on_new_email: new email — from=%s subject=%s message_id=%s",
            parsed.from_addr,
            parsed.subject,
            parsed.message_id,
        )

        already_exists = False
        try:
            async with Session() as session:
                async with session.begin():
                    # Idempotency: skip if already stored (can happen on IDLE restart).
                    existing = await session.execute(
                        select(GSageEmailMessage).where(
                            GSageEmailMessage.message_id == parsed.message_id,
                            GSageEmailMessage.org_id == account.org_id,
                        )
                    )
                    if existing.scalars().first() is not None:
                        logger.debug(
                            "EmailWorker._on_new_email: duplicate message_id — skipping — %s",
                            parsed.message_id,
                        )
                        already_exists = True
                    else:
                        # Resolve sender before persisting. Unknown senders are
                        # moved to a dedicated IMAP folder and not processed.
                        sender = await resolve_sender(
                            session, parsed.from_addr, account.org_id
                        )
                        if sender is None:
                            logger.warning(
                                "EmailWorker._on_new_email: unknown sender — from=%s "
                                "org_id=%s — moving to folder '%s'",
                                parsed.from_addr,
                                account.org_id,
                                settings.email_unknown_sender_folder,
                            )
                            try:
                                await imap_client.move_to_folder(
                                    uid, settings.email_unknown_sender_folder
                                )
                            except Exception as move_exc:
                                logger.error(
                                    "EmailWorker._on_new_email: move_to_folder failed — "
                                    "uid=%s error=%s",
                                    uid,
                                    move_exc,
                                )
                            return

                        email_msg = GSageEmailMessage(
                            org_id=account.org_id,
                            email_account_id=account.id,
                            message_id=parsed.message_id,
                            in_reply_to=parsed.in_reply_to,
                            references=" ".join(parsed.references) or None,
                            direction=GSageEmailDirection.INBOUND,
                            status=GSageEmailStatus.PENDING,
                            from_addr=parsed.from_addr,
                            to_addr=parsed.to_addr,
                            subject=parsed.subject,
                            body_text=parsed.body_text,
                            body_html=parsed.body_html,
                        )
                        session.add(email_msg)

        except Exception as exc:
            # Detect race: account was deleted between IMAP fetch and DB persist.
            # The IMAP client still holds an open session against the deleted
            # account, so messages briefly continue to arrive. Log as WARNING
            # (not ERROR) since this is benign and the per-account loop will
            # exit on its next reconnect cycle (account.get returns None).
            err_text = str(exc)
            if (
                "ForeignKeyViolationError" in err_text
                and "email_account_id" in err_text
            ):
                logger.warning(
                    "EmailWorker._on_new_email: account %s was deleted while "
                    "IMAP session was still active — discarding message %s. "
                    "Worker will stop polling this account on next reconnect.",
                    account_id,
                    parsed.message_id,
                )
                return
            logger.error(
                "EmailWorker._on_new_email: DB persist failed — message_id=%s error=%s",
                parsed.message_id,
                exc,
            )
            return

        # Already in DB: just ensure it's marked seen to stop re-fetching.
        if already_exists:
            await imap_client.mark_seen(uid)
            return

        # Dispatch Celery task (fire-and-forget; idempotency handled in task).
        dispatched = False
        try:
            process_email_inbound.apply_async(
                kwargs={
                    "message_id": parsed.message_id,
                    "org_id": str(account.org_id),
                },
                queue="email",
            )
            dispatched = True
        except Exception as exc:
            logger.error(
                "EmailWorker._on_new_email: Celery dispatch failed — message_id=%s error=%s",
                parsed.message_id,
                exc,
            )

        if dispatched:
            # Mark as \Seen (default) or permanently delete from IMAP server.
            if settings.email_delete_after_process:
                await imap_client.delete_message(uid)
            else:
                await imap_client.mark_seen(uid)


# ── Signal handling & entry point ─────────────────────────────────────────


_worker: Optional[EmailWorker] = None


def _handle_signal(sig, frame) -> None:
    logger.info("EmailWorker: received signal %s, shutting down...", sig)
    if _worker:
        _worker.stop()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _worker = EmailWorker()
    try:
        _worker.start()
    except KeyboardInterrupt:
        _worker.stop()
