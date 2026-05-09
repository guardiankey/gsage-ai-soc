"""Custom Gunicorn worker that forces the standard asyncio event loop.

Background
----------
The default ``uvicorn.workers.UvicornWorker`` uses ``loop="auto"``, which
prefers ``uvloop`` when installed. We hit a reproducible SIGSEGV inside
the SSL/socket teardown path when an SSE stream from an upstream LLM
provider is interrupted by client disconnect (``GeneratorExit``
propagating through the openai async stream). The crash trace points at
``uvloop.loop`` / ``httptools`` C extensions; switching to the standard
``asyncio`` event loop avoids the buggy code path while keeping the rest
of the stack intact.

Usage
-----
``gunicorn -k src.backend_api.app.gunicorn_worker.AsyncioUvicornWorker ...``
"""

from __future__ import annotations

from uvicorn.workers import UvicornWorker


class AsyncioUvicornWorker(UvicornWorker):
    """UvicornWorker pinned to the stdlib asyncio event loop."""

    CONFIG_KWARGS = {**UvicornWorker.CONFIG_KWARGS, "loop": "asyncio"}
