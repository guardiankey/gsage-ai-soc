"""gSage AI — Interaction API routes.

Endpoints for submitting and cancelling user interactions (forms, confirmations, …).
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend_api.app.api.deps import get_tenant_context
from src.backend_api.app.core.tenant import TenantContext
from src.backend_api.app.schemas.interaction import (
    InteractionCancelRequest,
    InteractionStatusResponse,
    InteractionSubmitRequest,
)
from src.backend_api.app.services.interaction_handler import (
    cancel_interaction,
    resolve_and_dispatch_interaction_response,
)
from src.shared.database import get_db

log = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/orgs/{org_id}/interactions/{interaction_id}/submit",
    response_model=InteractionStatusResponse,
    summary="Submit responses for a pending interaction",
)
async def submit_interaction(
    org_id: uuid.UUID,
    interaction_id: uuid.UUID,
    payload: InteractionSubmitRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InteractionStatusResponse:
    """Submit user responses for a pending interaction.

    - Validates org ownership of the interaction.
    - Persists the responses and marks the interaction as ``submitted``.
    - Dispatches the response to the waiting tool via the Redis broker.
    - For REPLAN_AGENT interactions, also injects a ``[INTERACTION_RESPONSE]``
      block into the agent session for replanning.
    """
    ctx.require_permission("agents:run")

    try:
        resume_mode = await resolve_and_dispatch_interaction_response(
            interaction_id=interaction_id,
            org_id=org_id,
            responses=payload.responses,
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND
            if "not found" in str(exc).lower()
            else status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    # REPLAN_AGENT: inject responses into the agent session so it can replan.
    # The [INTERACTION_RESPONSE] block follows the same pattern as
    # [BACKGROUND_TASKS_COMPLETED] — it's prepended to the next user message.
    if resume_mode == "replan_agent":
        await _inject_replan_context(
            interaction_id=interaction_id,
            responses=payload.responses,
            org_id=org_id,
            db=db,
            ctx=ctx,
        )

    return InteractionStatusResponse(
        interaction_id=str(interaction_id),
        status="submitted",
    )


@router.post(
    "/orgs/{org_id}/interactions/{interaction_id}/cancel",
    response_model=InteractionStatusResponse,
    summary="Cancel a pending interaction",
)
async def cancel_interaction_endpoint(
    org_id: uuid.UUID,
    interaction_id: uuid.UUID,
    payload: InteractionCancelRequest,  # noqa: ARG001 — kept for schema completeness
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InteractionStatusResponse:
    """Cancel a pending interaction (user dismissed the modal, etc.).

    Unblocks the waiting tool with an ``InteractionCancelled`` sentinel.
    """
    ctx.require_permission("agents:run")

    try:
        await cancel_interaction(
            interaction_id=interaction_id,
            org_id=org_id,
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND
            if "not found" in str(exc).lower()
            else status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    return InteractionStatusResponse(
        interaction_id=str(interaction_id),
        status="cancelled",
    )


# ── Internal helpers ──────────────────────────────────────────────────────


async def _inject_replan_context(
    *,
    interaction_id: uuid.UUID,
    responses: dict,  # noqa: ARG001 — kept for future use / audit
    org_id: uuid.UUID,  # noqa: ARG001 — kept for future use / audit
    db: AsyncSession,  # noqa: ARG001 — kept for future use / audit
    ctx: TenantContext,  # noqa: ARG001 — kept for future use / audit
) -> None:
    """Dispatch a Celery task to continue the agent after REPLAN_AGENT.

    The task (:func:`continue_after_interaction_submitted`) rebuilds the
    agent, constructs an ``[INTERACTION_RESPONSE]`` block from the
    persisted interaction record, and calls ``agent.arun()`` so the agent
    can replan with the user's responses.
    """
    try:
        from typing import cast, Any
        from src.backend_api.app.tasks.agent_continuation import (
            continue_after_interaction_submitted,
        )
        cast(Any, continue_after_interaction_submitted).delay(str(interaction_id))
        log.info(
            "REPLAN_AGENT interaction %s submitted — dispatched agent continuation",
            interaction_id,
        )
    except Exception:
        log.error(
            "Failed to dispatch continuation for interaction %s",
            interaction_id, exc_info=True,
        )
