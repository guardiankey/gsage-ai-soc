"""Agno session lock & pub/sub helpers.

Provides a distributed advisory lock (Redis ``SET NX EX``) scoped to a
single Agno session id, plus a small pub/sub helper used to notify
subscribed SSE clients that the conversation history has new content.

Background
----------
Two independent paths can invoke ``agent.arun()`` against the same
``agno_session_id`` concurrently:

* The chat SSE endpoint (user message) — :func:`stream_message`.
* The Celery continuation for a finished background tool —
  :func:`continue_after_bg_task`.

Because Agno persists the message history at the end of each ``arun()``,
two concurrent runs on the same session race to overwrite each other's
snapshot, producing the symptom where the latest user/assistant messages
"disappear" once the background tool returns.

This module is the serialization primitive used to prevent that race
without blocking unrelated conversations.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from typing import AsyncIterator, Optional

import redis.asyncio as redis

from src.shared.config.settings import get_settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (overridable via env)
# ---------------------------------------------------------------------------

_DEFAULT_LOCK_TTL = int(os.getenv("AGNO_SESSION_LOCK_TTL_SECONDS", "120"))
_DEFAULT_WAIT_TIMEOUT = float(os.getenv("AGNO_SESSION_LOCK_WAIT_SECONDS", "5"))
_POLL_INTERVAL = 0.05  # seconds — local poll between lock attempts while waiting

# Pub/Sub channel namespace.  Keep the prefix short and stable so the
# subscriber side can build the channel name deterministically from a
# conversation id (GSageTenantSession.id).
_CHANNEL_PREFIX = "gsage:conv-updates:"


# ---------------------------------------------------------------------------
# Redis client (module-level singleton, async)
# ---------------------------------------------------------------------------

_client: Optional[redis.Redis] = None
_client_loop_id: Optional[int] = None


def _get_client() -> redis.Redis:
    """Return a process-wide async Redis client (lazy).

    In Celery fork workers each task runs under a *fresh* ``asyncio.run()``
    loop. ``redis.asyncio`` connections (asyncio streams) are bound to the
    loop they were created on, so a cached client from a previous task
    holds dead transports and the next ``await client.set(...)`` raises
    ``RuntimeError: Event loop is closed`` / ``Future attached to a
    different loop``. We detect the loop change here and rebuild the
    client transparently — same pattern used in
    ``src/shared/database.py`` for the SQLAlchemy async engine.
    """
    global _client, _client_loop_id

    try:
        current_loop_id: Optional[int] = id(asyncio.get_running_loop())
    except RuntimeError:
        current_loop_id = None

    if (
        _client is not None
        and current_loop_id is not None
        and _client_loop_id is not None
        and _client_loop_id != current_loop_id
    ):
        # Drop the stale client without touching its connections — the old
        # event loop is already closed and awaiting close() on it would
        # raise. Leak the underlying sockets; the OS will reclaim them.
        _client = None

    if _client is None:
        settings = get_settings()
        _client = redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        _client_loop_id = current_loop_id
    return _client


# ---------------------------------------------------------------------------
# Lock primitive
# ---------------------------------------------------------------------------

class LockAcquireError(RuntimeError):
    """Raised when a session lock cannot be acquired within the wait window."""


def _lock_key(agno_session_id: str) -> str:
    return f"gsage:agno-session-lock:{agno_session_id}"


async def _try_acquire(
    client: redis.Redis,
    key: str,
    token: str,
    ttl_seconds: int,
) -> bool:
    """Atomic SET NX EX. Returns True if the caller now owns the lock."""
    res = await client.set(key, token, nx=True, ex=ttl_seconds)
    return bool(res)


# Lua script ensures we only release the lock when we still own it
# (token matches), preventing release of a lock taken over after TTL
# expiry by another worker.
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


async def _release(client: redis.Redis, key: str, token: str) -> None:
    try:
        await client.eval(_RELEASE_SCRIPT, 1, key, token)  # type: ignore[arg-type]
    except BaseException as exc:
        # Catch CancelledError and KeyboardInterrupt too — swallowing
        # them here prevents cancel-scope corruption upstream (the
        # anyio cancel stack must never see a CancelledError escape
        # from a finally block).
        if isinstance(exc, Exception):
            log.warning("agno_session_lock: release failed key=%s: %s", key, exc)
        else:
            log.debug(
                "agno_session_lock: release interrupted key=%s: %s",
                key, type(exc).__name__,
            )


@contextlib.asynccontextmanager
async def acquire(
    agno_session_id: str,
    *,
    ttl_seconds: int = _DEFAULT_LOCK_TTL,
    wait_timeout: float = _DEFAULT_WAIT_TIMEOUT,
    owner: str = "unknown",
) -> AsyncIterator[None]:
    """Async context manager that acquires (with bounded wait) and releases
    the session lock.

    Parameters
    ----------
    agno_session_id:
        The session identifier to lock on.  Granularity is **per session**:
        unrelated conversations are not blocked.
    ttl_seconds:
        Maximum time the lock can be held before Redis auto-expires it.
        Acts as a safety net so a crashed holder cannot deadlock the session.
    wait_timeout:
        Maximum time (seconds) to wait for the lock to become available.
        Use ``0`` for a non-blocking try.
    owner:
        Free-form label for logging (e.g. ``"sse:stream_message"``).

    Raises
    ------
    LockAcquireError
        If the lock could not be acquired within ``wait_timeout``.
    """
    client = _get_client()
    key = _lock_key(agno_session_id)
    token = uuid.uuid4().hex
    deadline = asyncio.get_event_loop().time() + max(wait_timeout, 0.0)

    while True:
        if await _try_acquire(client, key, token, ttl_seconds):
            break
        now = asyncio.get_event_loop().time()
        if now >= deadline:
            raise LockAcquireError(
                f"could not acquire lock for {agno_session_id} within "
                f"{wait_timeout:.1f}s (owner={owner})"
            )
        await asyncio.sleep(_POLL_INTERVAL)

    log.debug(
        "agno_session_lock: acquired session=%s owner=%s ttl=%ds",
        agno_session_id, owner, ttl_seconds,
    )
    try:
        yield
    finally:
        await _release(client, key, token)
        log.debug(
            "agno_session_lock: released session=%s owner=%s",
            agno_session_id, owner,
        )


async def try_acquire(
    agno_session_id: str,
    *,
    ttl_seconds: int = _DEFAULT_LOCK_TTL,
    owner: str = "unknown",
) -> Optional[str]:
    """Non-blocking acquire.  Returns the release token or ``None`` if busy.

    Caller is responsible for invoking :func:`release` with the returned
    token.  Prefer the :func:`acquire` context manager when possible.
    """
    client = _get_client()
    key = _lock_key(agno_session_id)
    token = uuid.uuid4().hex
    if await _try_acquire(client, key, token, ttl_seconds):
        log.debug(
            "agno_session_lock: try_acquire OK session=%s owner=%s",
            agno_session_id, owner,
        )
        return token
    log.debug(
        "agno_session_lock: try_acquire BUSY session=%s owner=%s",
        agno_session_id, owner,
    )
    return None


async def release(agno_session_id: str, token: str) -> None:
    """Release a lock previously obtained via :func:`try_acquire`."""
    client = _get_client()
    await _release(client, _lock_key(agno_session_id), token)


# ---------------------------------------------------------------------------
# Pub/Sub — notify subscribed SSE clients that a conversation changed.
# ---------------------------------------------------------------------------

def _channel(conv_id: str | uuid.UUID) -> str:
    return f"{_CHANNEL_PREFIX}{conv_id}"


async def publish_conversation_updated(
    conv_id: str | uuid.UUID,
    *,
    reason: str = "updated",
) -> None:
    """Publish a "messages changed" event for *conv_id*.

    Subscribers (see :func:`subscribe_conversation_updates`) will receive
    the payload and can trigger an immediate refetch on the client.

    Best-effort: any Redis error is logged and swallowed — the polling
    fallback on the frontend will eventually pick up the change.
    """
    try:
        client = _get_client()
        await client.publish(_channel(conv_id), reason)
    except Exception as exc:
        log.warning(
            "agno_session_lock: publish updated failed conv=%s: %s",
            conv_id, exc,
        )


async def subscribe_conversation_updates(
    conv_id: str | uuid.UUID,
) -> AsyncIterator[str]:
    """Async iterator yielding update events for *conv_id*.

    The iterator runs until cancelled (e.g. the SSE client disconnects).
    Each yielded value is the ``reason`` string passed to
    :func:`publish_conversation_updated`.
    """
    client = _get_client()
    pubsub = client.pubsub()
    channel = _channel(conv_id)
    await pubsub.subscribe(channel)
    try:
        while True:
            # ``get_message`` with a small timeout lets the caller cancel
            # the task cleanly between polls.
            msg = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if msg is None:
                # Yield a keep-alive hint so the SSE wrapper can emit
                # periodic comments to keep the connection open through
                # idle proxies.
                yield ""
                continue
            data = msg.get("data")
            yield str(data) if data is not None else "updated"
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(channel)
        with contextlib.suppress(Exception):
            await pubsub.aclose()
