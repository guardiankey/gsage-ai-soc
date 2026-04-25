"""gSage AI — Backend API entry point."""

from __future__ import annotations

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
import random

log = logging.getLogger(__name__)

_DIVIDER = "=" * 70

async def _safe_setup_index(tracer):
    """Attempt to create the ES index template at startup.

    Uses asyncio.wait_for WITHOUT asyncio.shield so the underlying coroutine
    is properly cancelled on timeout — avoiding leaked background tasks that
    spin-retry against an unavailable ES instance and saturate the CPU.
    """
    log.info("Elasticsearch index template setup starting...")
    for attempt in range(3):
        try:
            # No asyncio.shield here — we WANT cancellation on timeout so the
            # ES client stops retrying and releases resources immediately.
            await asyncio.wait_for(tracer.setup_index_template(), timeout=10)
            log.info("Elasticsearch index template setup complete.")
            return
        except asyncio.TimeoutError:
            log.warning("ES index template setup timed out (attempt %d/3)", attempt + 1)
        except asyncio.CancelledError:
            log.warning("ES index template setup cancelled (attempt %d/3)", attempt + 1)
            raise  # propagate cancellation — do not retry
        except Exception as exc:
            log.warning("ES index template setup error (attempt %d/3): %s", attempt + 1, exc)

        if attempt < 2:
            # Exponential backoff with jitter: 2s, 4s, (no sleep on last attempt)
            delay = 2 * (attempt + 1) + random.uniform(0, 1)
            await asyncio.sleep(delay)

    log.error("ES index template setup failed after 3 attempts — traces will be skipped")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — runs startup and shutdown hooks."""
    # ── Elasticsearch index template ──────────────────────────────────────
    try:
        tracer = get_tracer()
        #await tracer.setup_index_template() # was causing 100% CPU loop
        #asyncio.create_task(_safe_setup_index(tracer)) # below is better
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
