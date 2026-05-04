"""gSage AI — Microsoft Teams webhook router.

Hosts the public webhook endpoint that the Microsoft Bot Framework calls
for every inbound Teams activity:

    POST /api/v1/channels/teams/{profile_id}/messages

Each org registers its own Azure Bot (App Registration) and stores the
``app_id`` / ``app_password`` / ``tenant_id`` triple inside the
``GSageInterfaceProfile.interface_config`` JSONB. The path parameter
``profile_id`` selects the profile (and therefore the org); the
``BotFrameworkAdapter`` validates the inbound JWT against that profile's
``app_id``, so a token signed for org A cannot reach a profile of org B.

A health endpoint (``GET .../health``) is exposed for Azure-side probes.

Outbound replies happen *inside* the turn callback via
``turn_context.send_activity``. Proactive outbound (alerts, scheduled
notifications) is handled by ``channel_sender._deliver_teams``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Path, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.config.settings import get_settings
from src.shared.models.interface_profile import GSageInterfaceProfile
from src.teams_handler.graph_client import GraphClient
from src.teams_handler.handler import handle_teams_turn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/channels/teams", tags=["Teams"])

# Per-process cache of (BotFrameworkAdapter, GraphClient) keyed by profile_id.
# Avoids rebuilding the JWT/JWKS validator on every webhook hit. Invalidated
# automatically when the profile's `app_id` rotates (cache key includes it).
_adapter_cache: dict[tuple[uuid.UUID, str, str], Any] = {}
_graph_cache: dict[tuple[uuid.UUID, str], GraphClient] = {}


@router.get(
    "/{profile_id}/health",
    summary="Microsoft Teams webhook health probe",
)
async def teams_health(
    profile_id: uuid.UUID = Path(..., description="InterfaceProfile UUID"),
) -> dict:
    """Lightweight probe for Azure-side health checks.

    Does **not** authenticate — it only confirms that the profile exists,
    is active, and that the webhook process is reachable.
    """
    profile = await _load_active_profile(profile_id)
    return {
        "status": "ok",
        "profile_id": str(profile.id),
        "interface": profile.interface,
        "is_active": profile.is_active,
    }


@router.post(
    "/{profile_id}/messages",
    summary="Microsoft Teams inbound webhook",
    status_code=status.HTTP_200_OK,
)
async def teams_messages(
    request: Request,
    profile_id: uuid.UUID = Path(..., description="InterfaceProfile UUID"),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """Receive an Activity from the Bot Framework and dispatch to the agent.

    The request body is a Bot Framework Activity (JSON). The
    ``Authorization`` header carries the signed JWT to validate.
    """
    # Lazy imports — keep botbuilder out of the hot import path of the
    # rest of the API.
    from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
    from botbuilder.schema import Activity

    profile = await _load_active_profile(profile_id)
    cfg = profile.interface_config or {}
    app_id = cfg.get("app_id")
    app_password = cfg.get("app_password")
    tenant_id = cfg.get("tenant_id")

    if not (app_id and app_password):
        logger.error(
            "teams_messages: profile %s missing app_id/app_password in "
            "interface_config",
            profile_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Teams profile is misconfigured (credentials missing).",
        )

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON body: {exc}",
        ) from exc

    activity = Activity().deserialize(body)
    auth_header = authorization or ""

    adapter = _get_adapter(
        profile_id=profile_id,
        app_id=str(app_id),
        app_password=str(app_password),
        tenant_id=str(tenant_id) if tenant_id else None,
        adapter_cls=BotFrameworkAdapter,
        settings_cls=BotFrameworkAdapterSettings,
    )
    graph = (
        _get_graph_client(profile_id, str(app_id), str(app_password), str(tenant_id))
        if tenant_id
        else None
    )

    async def _on_turn(turn_context):
        await handle_teams_turn(
            profile=profile,
            turn_context=turn_context,
            graph_client=graph,
        )

    try:
        await adapter.process_activity(activity, auth_header, _on_turn)
    except PermissionError as exc:
        # botbuilder raises PermissionError on JWT validation failure.
        logger.warning(
            "teams_messages: auth rejected — profile_id=%s err=%s",
            profile_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Bot Framework token",
        ) from exc
    except Exception:
        logger.exception(
            "teams_messages: adapter.process_activity raised — profile_id=%s",
            profile_id,
        )
        raise

    return {"status": "ok"}


# ── Helpers ────────────────────────────────────────────────────────────


async def _load_active_profile(profile_id: uuid.UUID) -> GSageInterfaceProfile:
    """Fetch the active Teams ``InterfaceProfile`` or raise 404."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as session:
            stmt = select(GSageInterfaceProfile).where(
                GSageInterfaceProfile.id == profile_id,
                GSageInterfaceProfile.interface == "teams",
                GSageInterfaceProfile.is_active.is_(True),
            )
            profile = (await session.execute(stmt)).scalars().first()
            if profile is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Teams profile not found or inactive",
                )
            # Detach so we can use it after the session is closed.
            session.expunge(profile)
            return profile
    finally:
        await engine.dispose()


def _get_adapter(
    *,
    profile_id: uuid.UUID,
    app_id: str,
    app_password: str,
    tenant_id: str | None,
    adapter_cls: Any,
    settings_cls: Any,
) -> Any:
    """Return a cached ``BotFrameworkAdapter`` for *profile_id*."""
    # Cache key includes tenant_id so a tenant rotation rebuilds the adapter.
    key = (profile_id, app_id, tenant_id or "")
    cached = _adapter_cache.get(key)
    if cached is not None:
        return cached

    # ``channel_auth_tenant`` is required for single-tenant Entra App
    # Registrations.  Without it the adapter falls back to the public Bot
    # Framework tenant (``botframework.com``) and AAD returns AADSTS700016
    # — "Application not found in directory 'Bot Framework'".
    bf_settings = settings_cls(
        app_id=app_id,
        app_password=app_password,
        channel_auth_tenant=tenant_id or None,
    )
    adapter = adapter_cls(bf_settings)

    async def _on_turn_error(turn_context, error):
        logger.exception(
            "Teams turn error — profile_id=%s err=%s", profile_id, error
        )

    adapter.on_turn_error = _on_turn_error
    _adapter_cache[key] = adapter
    return adapter


def _get_graph_client(
    profile_id: uuid.UUID,
    app_id: str,
    app_password: str,
    tenant_id: str,
) -> GraphClient:
    """Return a cached ``GraphClient`` for *profile_id*."""
    key = (profile_id, app_id)
    cached = _graph_cache.get(key)
    if cached is not None:
        return cached

    settings = get_settings()
    # Redis client is created on demand inside GraphClient via the handler;
    # passing None here keeps the GraphClient transport-agnostic and lets
    # the handler decide. Each profile gets its own in-process token cache.
    client = GraphClient(
        app_id=app_id,
        app_password=app_password,
        tenant_id=tenant_id,
        redis_client=None,
        email_cache_ttl=settings.teams_graph_email_cache_ttl,
    )
    _graph_cache[key] = client
    return client
