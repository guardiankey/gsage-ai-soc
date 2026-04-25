"""Shared helper to safely cleanup MCP sessions attached to an agno agent.

This module exists because the anyio cancel-scope used by the MCP
``streamable-http`` transport can enter a ``_deliver_cancellation``
busy-loop that pins the event loop at 100% CPU (see details below).
A single correct implementation is shared across ``backend_api``,
``telegram_worker``, ``email_worker``, and Celery tasks.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

MCP_CLEANUP_TIMEOUT = 5.0  # seconds to wait for graceful MCP session cleanup


async def cleanup_agent_mcp(agent: Any, *, timeout: float = MCP_CLEANUP_TIMEOUT) -> None:
    """Best-effort cleanup of MCP sessions attached to *agent*.

    Known issue: when the MCP streamable-http peer closes the socket
    without our httpx transport draining it (CLOSE-WAIT on our side),
    ``tool.close()`` enters the anyio cancel scope ``__aexit__`` and
    ``_deliver_cancellation`` busy-loops the event loop at 100% CPU —
    blocking the whole worker/process forever.

    ``asyncio.wait_for`` is NOT safe here: on timeout it cancels the
    inner task, and that cancel is precisely what triggers the
    busy-loop (the inner task never acknowledges the cancel).
    ``wait_for`` then waits forever for the cancelled task to finish,
    and our ``except TimeoutError`` is never reached.

    Strategy: race ``tool.close()`` against a timer using
    ``asyncio.wait`` (which does NOT cancel on timeout). If it doesn't
    finish in time, DETACH the task (no ``.cancel()``) and wipe MCP
    state so agno's later ``disconnect_mcp_tools`` sees nothing to
    clean up. The detached task will eventually get garbage-collected;
    the underlying httpx client/TCP socket is leaked until process
    exit, but that is vastly preferable to a pinned-CPU zombie.

    Safe to call multiple times and with agents that have no MCP tools.
    Never raises.
    """
    tools = getattr(agent, "tools", None) or []
    for tool in tools:
        is_mcp = hasattr(type(tool), "__mro__") and any(
            c.__name__ in ("MCPTools", "MultiMCPTools") for c in type(tool).__mro__
        )
        if not is_mcp:
            continue
        close_task: asyncio.Task | None = None
        try:
            close_task = asyncio.create_task(tool.close(), name="mcp-close")
            done, _pending = await asyncio.wait({close_task}, timeout=timeout)
            if close_task not in done:
                log.warning(
                    "MCP tool close() did not finish in %.1fs — detaching "
                    "and forcing state cleanup to avoid cancel busy-loop",
                    timeout,
                )
                # DO NOT call close_task.cancel(): cancel is what triggers
                # the anyio _deliver_cancellation busy-loop.
                # Clear session tracking so agno's finally is a no-op.
                try:
                    tool._run_sessions.clear()
                    tool._run_session_contexts.clear()
                    tool._initialized = False
                except Exception:
                    log.debug("MCP forced state cleanup failed", exc_info=True)
                # Swallow any future exception on the detached task to
                # avoid "Task exception was never retrieved" warnings.
                close_task.add_done_callback(
                    lambda t: (t.exception() if not t.cancelled() else None)
                )
            else:
                # Task completed — consume its exception (if any).
                exc = close_task.exception()
                if exc is not None:
                    log.debug("MCP tool close() error (ignored): %s", exc)
        except Exception:
            log.debug("MCP cleanup unexpected error (ignored)", exc_info=True)


__all__ = ["cleanup_agent_mcp", "MCP_CLEANUP_TIMEOUT"]
