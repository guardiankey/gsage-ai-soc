"""gSage AI — health check routes."""

from fastapi import APIRouter

from src.shared.config.settings import get_settings

router = APIRouter()


@router.get("/health", tags=["Health"])
async def health() -> dict:
    """Liveness probe — returns 200 when the service is up."""
    return {"status": "ok"}


@router.get("/config", tags=["Config"])
async def public_config() -> dict:
    """Returns public feature flags for the frontend (no auth required)."""
    settings = get_settings()
    return {"allow_self_register": settings.allow_self_register}
