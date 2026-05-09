"""Anyio CancelScope spin-guard.

Background
----------
The asyncio backend of anyio drives cancellation via
``CancelScope._deliver_cancellation`` which reschedules itself via
``loop.call_soon`` while it still has tasks "eligible" for cancellation
(see anyio/_backends/_asyncio.py).

If a contained task cannot honour cancellation (e.g. it is stuck inside a
synchronous C extension call such as a wedged gRPC channel poll, or its
``_fut_waiter`` is repeatedly resurrected without forward progress), this
becomes an unbounded busy-loop that pins the event loop at 100 % CPU.

Diagnosing this in production is hard because the loop is in pure Python /
C with no traces emitted.

What this module does
---------------------
``install_anyio_spin_guard()`` monkey-patches
``anyio._backends._asyncio.CancelScope._deliver_cancellation`` so that:

1. Each CancelScope instance gets a per-scope counter of consecutive
   re-arming retries.
2. When the counter exceeds ``threshold`` consecutive retries (default
   500) the wrapper logs detailed information about the scope (tasks,
   their stacks, the scope's host task) and forces ``should_retry`` to
   ``False`` — breaking the spin so the event loop can recover.
3. Any successful cancellation delivery (i.e. a real ``task.cancel()``
   call) resets the counter for that scope.

The guard is **opt-in** via env var ``ANYIO_SPIN_GUARD`` (``1`` to
enable).  Default thresholds are conservative; tune via env vars.

Environment variables
---------------------
- ``ANYIO_SPIN_GUARD``           1/0          Enable the guard (default: 1)
- ``ANYIO_SPIN_THRESHOLD``       int          Consecutive retries before
                                              breaking the loop (default: 500)
- ``ANYIO_SPIN_LOG_INTERVAL``    int          Log every N retries past the
                                              threshold while still spinning
                                              (default: 5000)

Limitations
-----------
- Forcing ``should_retry=False`` may leave a task uncancelled.  This is
  preferable to a CPU pin that takes the whole worker down: the
  uncancelled task can be observed in the log and dealt with.
- The patch must run BEFORE any anyio CancelScope is created.  Call
  ``install_anyio_spin_guard()`` at the very top of your application
  entry point, before importing FastAPI / Uvicorn / httpx / etc.
"""

from __future__ import annotations

import logging
import os
import traceback
from typing import Any

log = logging.getLogger(__name__)

_INSTALLED = False


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def install_anyio_spin_guard() -> None:
    """Install the ``CancelScope._deliver_cancellation`` spin guard.

    Idempotent.  Safe to call multiple times.  Honours the
    ``ANYIO_SPIN_GUARD`` env var (default on).
    """
    global _INSTALLED
    if _INSTALLED:
        return

    if not _env_bool("ANYIO_SPIN_GUARD", True):
        log.info("anyio spin-guard disabled via ANYIO_SPIN_GUARD=0")
        return

    threshold = _env_int("ANYIO_SPIN_THRESHOLD", 500)
    log_interval = _env_int("ANYIO_SPIN_LOG_INTERVAL", 5000)

    try:
        from anyio._backends import _asyncio as anyio_asyncio
    except Exception:
        log.warning("anyio._backends._asyncio not importable — spin-guard not installed")
        return

    CancelScope = anyio_asyncio.CancelScope
    original = CancelScope._deliver_cancellation

    def _format_task(task: Any) -> str:
        try:
            name = task.get_name()
        except Exception:
            name = repr(task)
        coro = getattr(task, "get_coro", lambda: None)()
        coro_repr = repr(coro)
        # Walk the coroutine frame stack for a compact location summary.
        frames: list[str] = []
        cr = coro
        while cr is not None and len(frames) < 20:
            frame = getattr(cr, "cr_frame", None) or getattr(cr, "gi_frame", None)
            if frame is None:
                break
            code = frame.f_code
            frames.append(f"{code.co_filename}:{frame.f_lineno} in {code.co_name}")
            cr = getattr(cr, "cr_await", None) or getattr(cr, "gi_yieldfrom", None)
        waiter = getattr(task, "_fut_waiter", None)
        try:
            waiter_done = waiter.done() if waiter is not None else None
        except Exception:
            waiter_done = None
        return (
            f"  task name={name!r} done={task.done()} "
            f"must_cancel={getattr(task, '_must_cancel', '?')} "
            f"waiter={type(waiter).__name__} waiter_done={waiter_done}\n"
            f"    coro={coro_repr}\n"
            "    stack:\n      "
            + "\n      ".join(frames or ["<no frames>"])
        )

    def _dump_scope(scope: Any) -> str:
        lines = [
            f"CancelScope id={id(scope):x} cancel_called={scope._cancel_called} "
            f"shield={getattr(scope, '_shield', '?')} "
            f"host_task={getattr(scope, '_host_task', None)!r}",
            f"  reason={getattr(scope, '_cancel_reason', None)!r}",
            f"  tasks=({len(scope._tasks)}):",
        ]
        for t in list(scope._tasks)[:10]:
            try:
                lines.append(_format_task(t))
            except Exception as exc:
                lines.append(f"  <error formatting task: {exc!r}>")
        if scope._child_scopes:
            lines.append(f"  child_scopes=({len(scope._child_scopes)})")
        return "\n".join(lines)

    def patched_deliver(self: Any, origin: Any) -> bool:  # noqa: ANN401
        # Counter is stored directly on the scope to avoid a global dict
        # (which would itself need locking).  Attribute name is unique
        # enough to not collide with anyio internals.
        try:
            count = getattr(self, "_spin_guard_count", 0)
        except Exception:
            count = 0

        # Fast-path: anyio's _deliver_cancellation unconditionally sets
        # should_retry=True for every task still in self._tasks, regardless
        # of whether the task is done().  In real-world traces we have seen
        # a finished host_task lingering in self._tasks (likely a race
        # between scope teardown and a sibling cancellation arriving via
        # StreamingResponse client-disconnect), which causes an infinite
        # call_soon spin pinning the loop at 100 % CPU.  Short-circuit when
        # the scope contains nothing actionable.
        try:
            tasks = self._tasks
            child_scopes = self._child_scopes
        except Exception:
            tasks = ()
            child_scopes = ()
        if tasks and all(getattr(t, "done", lambda: False)() for t in tasks) \
                and not child_scopes:
            try:
                self._spin_guard_count = 0
                if origin is self and getattr(self, "_cancel_handle", None) is not None:
                    self._cancel_handle = None
            except Exception:
                pass
            return False

        try:
            should_retry = original(self, origin)
        except Exception:
            # Reset counter on any internal error so we don't permanently
            # poison future deliveries on this scope.
            try:
                self._spin_guard_count = 0
            except Exception:
                pass
            raise

        if not should_retry:
            try:
                self._spin_guard_count = 0
            except Exception:
                pass
            return False

        count += 1
        try:
            self._spin_guard_count = count
        except Exception:
            return should_retry  # cannot track — let it through

        if count == threshold:
            log.error(
                "anyio CancelScope spin detected (%d consecutive retries) — "
                "breaking the loop and dumping scope state. "
                "This usually means a contained task is stuck in non-cancellable "
                "synchronous I/O (e.g. wedged gRPC/HTTP socket).\n%s",
                count,
                _dump_scope(self),
            )
            # Also dump a stack trace of the calling thread for context.
            log.error("anyio spin-guard caller stack:\n%s", "".join(traceback.format_stack()))
            # Break the spin: pretend nothing needs retrying.  The
            # uncancelled task remains, but the event loop is freed.
            return False

        if count > threshold and (count - threshold) % log_interval == 0:
            log.warning(
                "anyio CancelScope %x still spinning post-break (%d retries)",
                id(self), count,
            )

        return should_retry

    CancelScope._deliver_cancellation = patched_deliver  # type: ignore[method-assign]
    _INSTALLED = True
    log.info(
        "anyio spin-guard installed (threshold=%d, log_interval=%d)",
        threshold,
        log_interval,
    )
