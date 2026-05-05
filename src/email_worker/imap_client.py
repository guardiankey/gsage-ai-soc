"""gSage AI — IMAP client (IDLE + polling fallback) (Phase 7).

Wraps ``aioimaplib`` to provide:
  - TLS connection with exponential-backoff reconnect.
  - IMAP IDLE (RFC 2177) mode with 28-minute keepalive NOOP (server limit: 30 min).
  - Polling fallback for servers that do not support IDLE.
  - IMAP MOVE / COPY + EXPUNGE to relocate unknown-sender emails.
  - Auto-creation of the unknown-sender folder on first use.

Exponential back-off schedule (seconds): 2, 4, 8, 16, 32, 60 (cap).
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import uuid
from typing import Any, Awaitable, Callable, Optional

import aioimaplib  # type: ignore[import-untyped]

from src.shared.models.email_account import GSageEmailAccount

logger = logging.getLogger(__name__)

# IMAP IDLE should refresh every 28 minutes to stay within the 30-minute
# server-side inactivity timeout defined in RFC 2177.
_IDLE_REFRESH_SECONDS = 28 * 60

# Back-off cap.
_BACKOFF_MAX_SECONDS = 60

# Type alias for the callback invoked on each new raw email.
# Args: raw RFC 5322 bytes, account UUID, IMAP UID/sequence-number string.
RawEmailCallback = Callable[[bytes, uuid.UUID, str], Awaitable[None]]


class IMAPClientWrapper:
    """Async IMAP client for a single GSageEmailAccount.

    Usage::

        client = IMAPClientWrapper(account)
        await client.connect()
        await client.idle_loop(my_callback)   # or poll_loop(...)
    """

    def __init__(self, account: GSageEmailAccount) -> None:
        self._account = account
        self._client: Any = None  # aioimaplib.IMAP4_SSL | aioimaplib.IMAP4
        self._connected = False
        # Determined at connect-time from server CAPABILITY response.
        # aioimaplib refuses to send UID SEARCH if server lacks UIDPLUS,
        # so we probe capabilities and fall back to plain SEARCH when needed.
        self._uid_search_supported = True

    # ── Connection ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Establish and authenticate the IMAP connection."""
        acc = self._account
        logger.info(
            "IMAPClientWrapper: connecting — host=%s port=%d tls=%s account=%s",
            acc.imap_host,
            acc.imap_port,
            acc.imap_use_tls,
            acc.email,
        )
        if acc.imap_use_tls:
            ssl_context = ssl.create_default_context()
            if not acc.imap_verify_ssl:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            self._client = aioimaplib.IMAP4_SSL(
                host=acc.imap_host,
                port=acc.imap_port,
                ssl_context=ssl_context,
            )
        else:
            self._client = aioimaplib.IMAP4(
                host=acc.imap_host,
                port=acc.imap_port,
            )

        await self._client.wait_hello_from_server()

        # Authentication: OAuth2 (XOAUTH2) for Microsoft 365 / Exchange
        # Online and any provider that disabled basic auth, otherwise
        # plain LOGIN with the stored password.
        auth_method = getattr(acc, "auth_method", "basic")
        logger.info(
            "IMAPClientWrapper: authenticating — account=%s username=%s auth_method=%s",
            acc.email,
            acc.imap_username,
            auth_method,
        )
        if auth_method == "oauth2":
            from src.shared.services.oauth_token import get_access_token

            try:
                token = await get_access_token(acc)
            except Exception as exc:
                raise ConnectionError(
                    f"OAuth2 token acquisition failed for {acc.email}: {exc}"
                ) from exc
            logger.info(
                "IMAPClientWrapper: sending XOAUTH2 — account=%s username=%s "
                "token_len=%d token_prefix=%s",
                acc.email,
                acc.imap_username,
                len(token),
                token[:6] if token else "<empty>",
            )
            response = await self._client.xoauth2(acc.imap_username, token)
        else:
            response = await self._client.login(acc.imap_username, acc.imap_password)
        if response.result != "OK":
            decoded_lines = [
                line.decode(errors="replace") if isinstance(line, (bytes, bytearray)) else str(line)
                for line in (response.lines or [])
            ]
            if auth_method == "oauth2":
                logger.error(
                    "IMAPClientWrapper: XOAUTH2 rejected by server — account=%s "
                    "username=%s result=%s lines=%s. Common causes: "
                    "(1) imap_username must be the mailbox UPN (the address of "
                    "the mailbox being accessed), not the app-registration name; "
                    "(2) the Azure AD app must have the IMAP.AccessAsApp "
                    "*application* permission with admin consent granted; "
                    "(3) the service principal must have been authorised on "
                    "this mailbox via Exchange Online PowerShell "
                    "(New-ServicePrincipal + Add-MailboxPermission with "
                    "FullAccess).",
                    acc.email,
                    acc.imap_username,
                    response.result,
                    decoded_lines,
                )
            else:
                logger.error(
                    "IMAPClientWrapper: LOGIN rejected by server — account=%s "
                    "username=%s result=%s lines=%s",
                    acc.email,
                    acc.imap_username,
                    response.result,
                    decoded_lines,
                )
            raise ConnectionError(
                f"IMAP login failed for {acc.email}: {response.lines}"
            )

        await self._client.select(acc.imap_folder)
        self._connected = True
        logger.info("IMAPClientWrapper: authenticated — account=%s", acc.email)

        # aioimaplib parses CAPABILITY from the server greeting / post-LOGIN
        # untagged response and exposes has_capability() for callers.
        # Office 365 advertises UIDPLUS, so this drives whether we use UID-
        # based commands or plain commands.
        try:
            self._uid_search_supported = bool(self._client.has_capability("UIDPLUS"))
            logger.info(
                "IMAPClientWrapper: IMAP capabilities — account=%s uid_search=%s",
                acc.email,
                self._uid_search_supported,
            )
        except Exception as cap_exc:
            # If we cannot determine capabilities, assume UID SEARCH is NOT
            # supported — safer than assuming it is and crashing repeatedly.
            self._uid_search_supported = False
            logger.warning(
                "IMAPClientWrapper: capability inspection failed, assuming no UIDPLUS "
                "— account=%s error=%r",
                acc.email,
                cap_exc,
            )

    async def disconnect(self) -> None:
        """Logout and close the connection gracefully."""
        if self._client and self._connected:
            try:
                await self._client.logout()
            except Exception:
                pass
            self._connected = False
            self._client = None

    # ── IDLE loop ──────────────────────────────────────────────────────────

    async def idle_loop(
        self,
        callback: RawEmailCallback,
    ) -> None:
        """Run IMAP IDLE, calling *callback* for every new message.

        The loop refreshes the IDLE command every 28 minutes.
        Reconnects automatically using exponential back-off on errors.

        Args:
            callback: ``async (raw_bytes, account_id) -> None``
                      Called once per new message with its raw RFC 5322 bytes.
        """
        back_off = 2
        while True:
            try:
                await self._process_new_messages(callback)
                logger.debug(
                    "IMAPClientWrapper.idle_loop: entering IDLE — account=%s",
                    self._account.email,
                )
                idle_task = await self._client.idle_start(
                    timeout=_IDLE_REFRESH_SECONDS
                )
                await asyncio.wait_for(
                    self._client.wait_server_push(),
                    timeout=_IDLE_REFRESH_SECONDS,
                )
                self._client.idle_done()  # synchronous in aioimaplib
                await idle_task
                back_off = 2  # reset on success

                await self._process_new_messages(callback)

            except asyncio.TimeoutError:
                # Normal: IDLE timeout reached, restart.
                try:
                    self._client.idle_done()  # synchronous in aioimaplib
                except Exception:
                    pass
                logger.debug(
                    "IMAPClientWrapper.idle_loop: IDLE timeout, refreshing — account=%s",
                    self._account.email,
                )

            except Exception as exc:
                logger.error(
                    "IMAPClientWrapper.idle_loop: error — account=%s error=%s; "
                    "reconnecting in %ds",
                    self._account.email,
                    exc,
                    back_off,
                    exc_info=True,
                )
                await self.disconnect()
                await asyncio.sleep(back_off)
                back_off = min(back_off * 2, _BACKOFF_MAX_SECONDS)
                try:
                    await self.connect()
                except Exception as conn_exc:
                    logger.error(
                        "IMAPClientWrapper.idle_loop: reconnect failed — %s",
                        conn_exc,
                    )

    # ── Polling loop ───────────────────────────────────────────────────────

    async def poll_loop(
        self,
        callback: RawEmailCallback,
        interval: int,
    ) -> None:
        """Fallback polling loop when IDLE is not supported.

        Args:
            callback: Same as idle_loop.
            interval: Poll interval in seconds.
        """
        back_off = 2
        while True:
            try:
                await self._process_new_messages(callback)
                await asyncio.sleep(interval)
                back_off = 2

            except Exception as exc:
                logger.error(
                    "IMAPClientWrapper.poll_loop: error — account=%s error=%s; "
                    "reconnecting in %ds",
                    self._account.email,
                    exc,
                    back_off,
                    exc_info=True,
                )
                await self.disconnect()
                await asyncio.sleep(back_off)
                back_off = min(back_off * 2, _BACKOFF_MAX_SECONDS)
                try:
                    await self.connect()
                except Exception as conn_exc:
                    logger.error(
                        "IMAPClientWrapper.poll_loop: reconnect failed — %s",
                        conn_exc,
                    )

    # ── Folder management ─────────────────────────────────────────────────

    async def mark_seen(self, uid: str) -> None:
        """Mark a message as \\Seen so it is not re-fetched on reconnect.

        Uses UID STORE when UIDPLUS is supported, plain STORE otherwise.

        Args:
            uid: IMAP UID or sequence number (string).
        """
        try:
            if self._uid_search_supported:
                await self._client.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            else:
                await self._client.store(uid, "+FLAGS", "(\\Seen)")
            logger.debug(
                "IMAPClientWrapper.mark_seen: ok — uid=%s account=%s",
                uid, self._account.email,
            )
        except Exception as exc:
            logger.warning(
                "IMAPClientWrapper.mark_seen: failed — uid=%s account=%s error=%s",
                uid, self._account.email, exc,
            )

    async def delete_message(self, uid: str) -> None:
        """Permanently delete a message by flagging \\Deleted and expunging.

        Used when ``EMAIL_DELETE_AFTER_PROCESS=true``.

        Args:
            uid: IMAP UID or sequence number (string).
        """
        try:
            if self._uid_search_supported:
                await self._client.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            else:
                await self._client.store(uid, "+FLAGS", "(\\Deleted)")
            await self._client.expunge()
            logger.debug(
                "IMAPClientWrapper.delete_message: ok — uid=%s account=%s",
                uid, self._account.email,
            )
        except Exception as exc:
            logger.warning(
                "IMAPClientWrapper.delete_message: failed — uid=%s account=%s error=%s",
                uid, self._account.email, exc,
            )

    async def move_to_folder(self, uid: str, folder_name: str) -> None:
        """Move a message to *folder_name*.

        Tries IMAP MOVE (RFC 6851) first; falls back to COPY + \\Deleted +
        EXPUNGE for servers that do not support MOVE.
        Uses UID-based commands when UIDPLUS is supported, plain commands
        otherwise.

        Args:
            uid:         IMAP UID or sequence number (string).
            folder_name: Destination folder name (e.g., "Unknown-Senders").
        """
        logger.info(
            "IMAPClientWrapper.move_to_folder: starting — uid=%s dest=%s account=%s "
            "uid_mode=%s",
            uid, folder_name, self._account.email, self._uid_search_supported,
        )
        await self.create_folder_if_needed(folder_name)

        # Try RFC 6851 IMAP MOVE.
        try:
            if self._uid_search_supported:
                move_resp = await self._client.uid("MOVE", uid, folder_name)
            else:
                move_resp = await self._client.move(uid, folder_name)
        except Exception as exc:
            logger.warning(
                "IMAPClientWrapper.move_to_folder: MOVE raised — uid=%s dest=%s "
                "error=%r — will try COPY+EXPUNGE fallback",
                uid, folder_name, exc,
            )
            move_resp = None

        if move_resp is not None and move_resp.result == "OK":
            logger.info(
                "IMAPClientWrapper.move_to_folder: MOVE ok — uid=%s dest=%s",
                uid,
                folder_name,
            )
            return

        if move_resp is not None:
            logger.info(
                "IMAPClientWrapper.move_to_folder: MOVE returned %s — falling back "
                "to COPY+EXPUNGE — uid=%s dest=%s lines=%s",
                move_resp.result, uid, folder_name, move_resp.lines,
            )

        # Fallback: COPY + flag + expunge.
        if self._uid_search_supported:
            await self._client.uid("COPY", uid, folder_name)
            await self._client.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        else:
            await self._client.copy(uid, folder_name)
            await self._client.store(uid, "+FLAGS", "(\\Deleted)")
        await self._client.expunge()
        logger.info(
            "IMAPClientWrapper.move_to_folder: COPY+EXPUNGE fallback ok — "
            "uid=%s dest=%s",
            uid,
            folder_name,
        )

    async def create_folder_if_needed(self, folder_name: str) -> None:
        """Create *folder_name* on the server if it does not already exist."""
        list_resp = await self._client.list("", folder_name)
        # aioimaplib returns lines; an empty list means folder not found.
        exists = any(
            folder_name.encode() in line
            for line in list_resp.lines
            if isinstance(line, bytes)
        )
        if not exists:
            create_resp = await self._client.create(folder_name)
            if create_resp.result == "OK":
                logger.info(
                    "IMAPClientWrapper.create_folder_if_needed: created — folder=%s account=%s",
                    folder_name,
                    self._account.email,
                )

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _process_new_messages(self, callback: RawEmailCallback) -> None:
        """Fetch all UNSEEN messages and invoke *callback* for each.

        Uses ``UID SEARCH`` when supported; falls back to plain ``SEARCH``
        (returning sequence numbers) for servers that reject ``UID SEARCH``.
        """
        # Office 365 IMAP only accepts CHARSET=US-ASCII; aioimaplib's
        # search() defaults to UTF-8 and the server replies BADCHARSET.
        # Pass charset=None so the CHARSET token is omitted \u2014 "UNSEEN" is
        # pure ASCII so omitting CHARSET is always safe. The uid() raw-
        # command path never sends CHARSET, so it does not need the kwarg.
        if self._uid_search_supported:
            try:
                search_resp = await self._client.uid("SEARCH", "UNSEEN")
            except aioimaplib.Abort:
                # aioimaplib raises Abort client-side when server lacks UIDPLUS.
                # Switch permanently to plain SEARCH for this connection.
                logger.warning(
                    "IMAPClientWrapper._process_new_messages: UID SEARCH aborted by client "
                    "(server missing UIDPLUS) — switching to plain SEARCH — account=%s",
                    self._account.email,
                )
                self._uid_search_supported = False
                search_resp = await self._client.search("UNSEEN", charset=None)
            else:
                if search_resp.result != "OK":
                    logger.warning(
                        "IMAPClientWrapper._process_new_messages: UID SEARCH failed "
                        "— account=%s lines=%s",
                        self._account.email,
                        search_resp.lines,
                    )
                    return

        if not self._uid_search_supported:
            search_resp = await self._client.search("UNSEEN", charset=None)
            if search_resp.result != "OK":
                logger.warning(
                    "IMAPClientWrapper._process_new_messages: SEARCH failed — account=%s lines=%s",
                    self._account.email,
                    search_resp.lines,
                )
                return

        # Parse UID/sequence-number list from response lines.
        ids: list[str] = []
        for part in search_resp.lines:
            if isinstance(part, bytes):
                tokens = part.decode(errors="replace").split()
                ids.extend(t for t in tokens if t.isdigit())
            elif isinstance(part, str):
                ids.extend(t for t in part.split() if t.isdigit())

        logger.debug(
            "IMAPClientWrapper._process_new_messages: found %d UNSEEN message(s) "
            "— account=%s uid_search=%s ids=%s",
            len(ids),
            self._account.email,
            self._uid_search_supported,
            ids,
        )

        for msg_id in ids:
            try:
                if self._uid_search_supported:
                    fetch_resp = await self._client.uid("FETCH", msg_id, "(RFC822)")
                else:
                    fetch_resp = await self._client.fetch(msg_id, "(RFC822)")
                raw = _extract_rfc822(fetch_resp)
                if raw:
                    # Pass msg_id (UID or seq number) so the callback can
                    # mark/move/delete the message on the IMAP server.
                    await callback(raw, self._account.id, msg_id)
                else:
                    logger.warning(
                        "IMAPClientWrapper._process_new_messages: empty RFC822 body "
                        "— account=%s id=%s fetch_lines=%s",
                        self._account.email,
                        msg_id,
                        fetch_resp.lines,
                    )
            except aioimaplib.CommandTimeout:
                # Server stopped responding (often after a BYE on the
                # previous command). The connection is dead — abort the
                # batch and let idle_loop reconnect. Re-raising preserves
                # the existing reconnect/back-off path.
                logger.warning(
                    "IMAPClientWrapper._process_new_messages: FETCH timed out — "
                    "connection likely dead, triggering reconnect — account=%s id=%s",
                    self._account.email,
                    msg_id,
                )
                self._connected = False
                raise
            except Exception as exc:
                logger.error(
                    "IMAPClientWrapper._process_new_messages: fetch failed — account=%s id=%s error=%s",
                    self._account.email,
                    msg_id,
                    exc,
                    exc_info=True,
                )


# ── Module helpers ─────────────────────────────────────────────────────────


def _extract_rfc822(fetch_response: aioimaplib.Response) -> Optional[bytes]:
    """Extract the raw RFC 822 email bytes from a FETCH response."""
    lines = fetch_response.lines
    # aioimaplib stores body as element in lines list (bytes or string entries).
    for entry in lines:
        if isinstance(entry, (bytes, bytearray)) and len(entry) > 200:
            return bytes(entry)
    return None
