"""gSage AI — Telegram Bot Worker entry point.

Architecture
------------
One python-telegram-bot ``Application`` is spawned per unique ``bot_token``
found in active InterfaceProfiles of type ``telegram``.

Each Application:
  1. Registers a ``MessageHandler`` for text messages.
  2. Starts long-polling via ``Application.run_polling()``.
  3. Dispatches each inbound message to ``handler.handle_message()``.

Messages are processed **inline** (no Celery) inside the asyncio handler
for low-latency responses.

Hot-reload: every ``TELEGRAM_RELOAD_INTERVAL`` seconds the worker re-queries
active InterfaceProfiles, starts new bots for newly added tokens, and stops
bots whose tokens are no longer active.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Optional

from src.shared.logging import configure_logging

# Configure structured JSON logging before anything else
configure_logging("telegram_worker")

logger = logging.getLogger(__name__)


class TelegramWorker:
    """Async Telegram bot worker — one Application per unique bot token."""

    def __init__(self) -> None:
        self._running = False
        # Maps bot_token -> (Application, asyncio.Task)
        self._bots: dict[str, tuple] = {}
        self._reload_task: Optional[asyncio.Task] = None
        # Shared DB engine / session factory reused across all bots and
        # profile reloads.  Created once at start-up, stored in each
        # Application's bot_data for handler access, disposed at shutdown.
        self._db_engine = None
        self._db_session_factory = None

    # ── Public interface ──────────────────────────────────────────────────

    def start(self) -> None:
        """Entry point (blocking). Runs the asyncio event loop."""
        self._running = True
        logger.info("TelegramWorker: starting...")
        asyncio.run(self._run())

    def stop(self) -> None:
        """Signal the worker to stop gracefully."""
        logger.info("TelegramWorker: stop requested")
        self._running = False
        if self._reload_task and not self._reload_task.done():
            self._reload_task.cancel()
        for token, (app, task) in list(self._bots.items()):
            task.cancel()

    # ── Core asyncio loop ─────────────────────────────────────────────────

    async def _run(self) -> None:
        from src.shared.config.settings import get_settings
        from src.shared.database import create_pooled_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession as _AsyncSession

        settings = get_settings()

        # Create a single shared engine + session factory for the lifetime
        # of this worker.  Every bot Application and the profile-reload
        # polling loop reuse this engine, avoiding per-message pool churn.
        if self._db_engine is None:
            self._db_engine = create_pooled_engine(settings)
            self._db_session_factory = async_sessionmaker(
                self._db_engine, class_=_AsyncSession, expire_on_commit=False,
            )

        # Initial load
        profiles = await self._load_active_profiles()
        if not profiles:
            logger.info(
                "TelegramWorker: no active Telegram InterfaceProfiles found — "
                "waiting (polling every 60s)..."
            )
            while self._running:
                await asyncio.sleep(60)
                profiles = await self._load_active_profiles()
                if profiles:
                    logger.info(
                        "TelegramWorker: %d profile(s) now available — starting bots",
                        len(profiles),
                    )
                    break
            if not profiles:
                logger.info("TelegramWorker: shutting down — no profiles and stop requested")
                return

        await self._sync_bots(profiles)

        # Hot-reload loop
        reload_interval = settings.telegram_reload_interval
        if reload_interval > 0:
            self._reload_task = asyncio.create_task(
                self._hot_reload_loop(reload_interval),
                name="tg-hot-reload",
            )

        # Wait until all bot tasks finish (cancelled on stop)
        all_tasks = [task for _, (_, task) in self._bots.items()]
        if self._reload_task:
            all_tasks.append(self._reload_task)

        try:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            await self._stop_all_bots()
            # Dispose the shared DB engine so all asyncpg connections are
            # properly closed before the event loop shuts down.
            if self._db_engine is not None:
                try:
                    await self._db_engine.dispose()
                except Exception:
                    pass
                self._db_engine = None
                self._db_session_factory = None
            logger.info("TelegramWorker: stopped")

    # ── Profile loading ───────────────────────────────────────────────────

    async def _load_active_profiles(self) -> list:
        """Return all active Telegram InterfaceProfiles from the DB."""
        from src.shared.models.interface_profile import GSageInterfaceProfile
        from sqlalchemy import select

        if self._db_session_factory is None:
            raise RuntimeError("DB session factory not initialised — call _run() first")

        try:
            async with self._db_session_factory() as session:
                result = await session.execute(
                    select(GSageInterfaceProfile).where(
                        GSageInterfaceProfile.interface == "telegram",
                        GSageInterfaceProfile.is_active == True,  # noqa: E712
                    )
                )
                profiles = result.scalars().all()
                return list(profiles)
        except Exception as exc:
            logger.error("TelegramWorker._load_active_profiles: error — %s", exc)
            return []

    # ── Bot lifecycle ─────────────────────────────────────────────────────

    async def _sync_bots(self, profiles: list) -> None:
        """Start/stop bot Applications to match *profiles* (by unique bot_token)."""
        from src.telegram_worker.handler import build_application

        # Collect unique bot tokens from profiles
        active_tokens: dict[str, list] = {}
        for profile in profiles:
            cfg = profile.interface_config or {}
            token = cfg.get("bot_token", "").strip()
            if not token:
                logger.warning(
                    "TelegramWorker._sync_bots: profile %s has no bot_token — skipping",
                    profile.id,
                )
                continue
            active_tokens.setdefault(token, []).append(profile)

        # Stop bots whose token is no longer active
        for token in list(self._bots.keys()):
            if token not in active_tokens:
                logger.info("TelegramWorker: stopping removed bot (token=...%s)", token[-6:])
                app, task = self._bots.pop(token)
                task.cancel()
                try:
                    await app.stop()
                    await app.shutdown()
                except Exception:
                    pass

        # Start new bots
        for token, token_profiles in active_tokens.items():
            if token in self._bots:
                continue  # already running
            logger.info(
                "TelegramWorker: starting bot for token ...%s (%d profile(s))",
                token[-6:],
                len(token_profiles),
            )
            try:
                app = build_application(token, token_profiles)
                # Inject the shared DB engine so the handler can reuse it
                # instead of creating a new pool per inbound message.
                app.bot_data["db_engine"] = self._db_engine
                app.bot_data["db_session_factory"] = self._db_session_factory
                task = asyncio.create_task(
                    self._run_bot(app, token),
                    name=f"tg-bot-{token[-6:]}",
                )
                self._bots[token] = (app, task)
            except Exception as exc:
                logger.error(
                    "TelegramWorker._sync_bots: could not start bot ...%s — %s",
                    token[-6:],
                    exc,
                )

    async def _run_bot(self, app, token: str) -> None:
        """Run a single python-telegram-bot Application (long-polling)."""
        back_off = 2
        while self._running and token in self._bots:
            try:
                await app.initialize()
                await app.start()
                await app.updater.start_polling(drop_pending_updates=True)
                logger.info("TelegramWorker._run_bot: polling started — ...%s", token[-6:])
                back_off = 2
                # Block until cancelled
                while self._running and token in self._bots:
                    await asyncio.sleep(1)
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    "TelegramWorker._run_bot: error — ...%s %s; reconnecting in %ds",
                    token[-6:],
                    exc,
                    back_off,
                )
                await asyncio.sleep(back_off)
                back_off = min(back_off * 2, 60)
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            pass
        logger.info("TelegramWorker._run_bot: stopped — ...%s", token[-6:])

    async def _stop_all_bots(self) -> None:
        """Gracefully stop all running bots."""
        for token, (app, task) in list(self._bots.items()):
            task.cancel()
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception:
                pass
        self._bots.clear()

    # ── Hot-reload loop ───────────────────────────────────────────────────

    async def _hot_reload_loop(self, interval: int) -> None:
        """Periodically re-sync bots with the DB."""
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break
            logger.debug("TelegramWorker: hot-reload — re-querying active profiles")
            profiles = await self._load_active_profiles()
            await self._sync_bots(profiles)


# ── Entry point ───────────────────────────────────────────────────────────────

_worker: Optional[TelegramWorker] = None


def _handle_signal(signum: int, frame) -> None:
    logger.info("TelegramWorker: received signal %d — shutting down", signum)
    if _worker is not None:
        _worker.stop()


def main() -> None:
    global _worker
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _worker = TelegramWorker()
    _worker.start()
    sys.exit(0)


if __name__ == "__main__":
    main()
