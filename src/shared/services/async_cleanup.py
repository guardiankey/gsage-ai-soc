"""Async resource cleanup helpers for Celery / one-shot ``asyncio.run()``.

Celery's ForkPoolWorker creates a fresh event loop for every ``asyncio.run()``
invocation and closes it as soon as the coroutine returns. Any object that
holds non-disposed async resources (``httpx.AsyncClient`` connection pools
created by the OpenAI / DeepSeek / Anthropic / agno LLM SDKs, etc.) becomes
unreachable only after the loop is gone, so its ``__del__`` schedules
``aclose()`` on the dead loop and produces noisy ``RuntimeError: Event loop is
closed`` tracebacks on stderr.

This module sweeps the GC's object list, finds any open
``httpx.AsyncClient`` instance, and closes it cleanly *before* the loop ends.
"""

from __future__ import annotations

import gc
import logging
from typing import Any

log = logging.getLogger(__name__)


async def close_lingering_httpx_clients(timeout: float = 2.0) -> None:
    """Close every still-open ``httpx.AsyncClient`` reachable via GC.

    Best-effort: never raises. Safe to call from a Celery task ``finally``
    block right before ``asyncio.run()`` returns.

    The agno LLM model wrappers (OpenAIChat, DeepSeek, Claude, Gemini) keep
    their underlying ``openai.AsyncOpenAI`` / ``anthropic.AsyncAnthropic``
    HTTP clients alive on the model instance; those clients in turn own a
    ``httpx.AsyncClient`` whose ``__del__`` schedules ``aclose()`` on the GC
    thread / closed loop. Closing them here avoids that.
    """
    try:
        import httpx  # noqa: PLC0415
    except ImportError:
        return

    import asyncio  # noqa: PLC0415

    clients: list[Any] = []
    for obj in gc.get_objects():
        try:
            if isinstance(obj, httpx.AsyncClient) and not obj.is_closed:
                clients.append(obj)
        except Exception:
            # Some proxy / weak-ref objects raise when isinstance-checked.
            continue

    if not clients:
        return

    log.debug("close_lingering_httpx_clients: closing %d client(s)", len(clients))
    coros = [c.aclose() for c in clients]
    try:
        await asyncio.wait_for(
            asyncio.gather(*coros, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.warning(
            "close_lingering_httpx_clients: timed out closing %d client(s)",
            len(clients),
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("close_lingering_httpx_clients: ignored error: %s", exc)
