"""gSage AI — Backend API entry point."""

from __future__ import annotations

# Enable faulthandler at startup so SIGSEGV / SIGABRT in C extensions emit a
# Python + C stack trace to stderr before the worker dies. Helps diagnose
# crashes inside _ssl, _asyncio, grpc, etc.
import faulthandler
import sys

faulthandler.enable(file=sys.stderr, all_threads=True)

# Install the anyio CancelScope spin-guard BEFORE any module that may use
# anyio cancel scopes (FastAPI, Starlette, httpx, weaviate-client, ...).
# This protects the worker from a CPU pin caused by ``_deliver_cancellation``
# busy-loops when a task is stuck in non-cancellable synchronous I/O.
from src.shared.diagnostics.anyio_spin_guard import install_anyio_spin_guard

install_anyio_spin_guard()

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.backend_api.app.api.middleware.rate_limit import RateLimitHeadersMiddleware
from src.backend_api.app.api.v1.router import api_router
from src.backend_api.app.services.elasticsearch import get_tracer
from src.shared.bootstrap import ensure_admin
from src.shared.config.settings import get_settings
from src.shared.database import _get_session_maker
import logging
import asyncio

log = logging.getLogger(__name__)

_DIVIDER = "=" * 70


async def _safe_setup_index(tracer) -> None:
    """Best-effort ES index template setup.

    Runs the underlying sync Elasticsearch client in a worker thread (the
    async wrapper in ``ElasticsearchTracer.setup_index_template`` uses
    ``asyncio.to_thread``).  We deliberately do **NOT** wrap this in
    ``asyncio.wait_for`` — past attempts caused the anyio
    ``_deliver_cancellation`` busy-loop to pin a worker at 100% CPU when the
    target task could not honour cancellation promptly (sync I/O inside C
    extensions).  Any failure is logged and swallowed.
    """
    try:
        await tracer.setup_index_template()
        log.info("Elasticsearch index template setup complete.")
    except Exception:
        log.warning("ES index template setup failed — traces will be skipped", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — runs startup and shutdown hooks."""
    # ── Elasticsearch index template ──────────────────────────────────────
    try:
        tracer = get_tracer()
        task = asyncio.create_task(_safe_setup_index(tracer))
        task.add_done_callback(
            lambda t: log.error("setup_index task crashed", exc_info=t.exception()) if t.exception() else None
        )
    except Exception:
        log.warning("Elasticsearch tracer setup failed — traces will be skipped", exc_info=True)

    # ── Admin bootstrap seed ──────────────────────────────────────────────
    try:
        async with _get_session_maker()() as session:
            raw_key = await ensure_admin(session)
        if raw_key:
            log.info(_DIVIDER)
            log.info("BOOTSTRAP ADMIN API KEY — shown ONCE, store it now!")
            log.info("  %s", raw_key)
            log.info(_DIVIDER)
    except Exception:
        log.exception("Admin bootstrap seed failed — continuing startup")

    yield

    # ── Shutdown hooks ────────────────────────────────────────────────────
    # Close the shared Weaviate async client so httpx/grpc sockets are
    # released cleanly. Leaving them open on shutdown can keep sockets in
    # CLOSE-WAIT on the peer side and contribute to stale-connection issues
    # after restart.
    try:
        from src.shared.weaviate_client import close_weaviate_client

        await close_weaviate_client()
    except Exception:
        log.warning("Weaviate client close failed at shutdown", exc_info=True)




def create_app() -> FastAPI:
    settings = get_settings()

    # Configure structured logging (stdout JSON + ES handler) for the backend
    # service. Without this call the root logger stays at WARNING level and
    # logger.info(...) messages from src.shared.* are silently dropped.
    from src.shared.logging import configure_logging  # noqa: PLC0415

    configure_logging(
        service_name="backend",
        level="DEBUG" if settings.debug else "INFO",
    )

    app = FastAPI(
        title="gSage AI",
        version="1.0.0",
        description="Multi-tenant AI assistant backend — V1 Foundation",
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        lifespan=lifespan,
    )

    # CORS — outermost layer
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Needs-Polling", "X-Has-Pending-Approvals"],
    )

    # Rate-limit response headers (reads request.state.rl_* set by the
    # check_rate_limit FastAPI dependency registered on org-scoped routes)
    app.add_middleware(RateLimitHeadersMiddleware)

    app.include_router(api_router, prefix="/api")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
