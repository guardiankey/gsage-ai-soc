"""gSage AI — Interaction Service: backend-side orchestration.

Handles REPLAN_AGENT resume logic: when a user submits a form whose
resume mode is ``replan_agent``, the responses are injected into the
agent session as a ``[INTERACTION_RESPONSE]`` context block so the
agent can replan with the new information.

Also provides the Redis-backed response/cancellation dispatch used by
the REST endpoints to unblock waiting CONTINUE_TOOL tool executions.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import uuid
from typing import Optional

import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config.settings import get_settings
from src.shared.interaction.broker_redis import RedisBroker
from src.shared.models.gsage_interaction import GSageInteraction

log = logging.getLogger(__name__)

# ── Redis client (lazy, cached per event loop) ──────────────────────────

_redis_client: Optional[redis.Redis] = None
_redis_loop_id: Optional[int] = None


def _get_redis() -> redis.Redis:
    """Return a module-level async Redis client, recreated on loop change."""
    global _redis_client, _redis_loop_id
    try:
        current_loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        current_loop_id = -1

    if _redis_client is not None and _redis_loop_id == current_loop_id:
        return _redis_client

    settings = get_settings()
    _redis_client = redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    _redis_loop_id = current_loop_id
    return _redis_client


# ── Public API ───────────────────────────────────────────────────────────


async def resolve_and_dispatch_interaction_response(
    interaction_id: uuid.UUID,
    org_id: uuid.UUID,
    responses: dict,
    db: AsyncSession,
) -> str:
    """Validate ownership, persist the response, and dispatch to the broker.

    Returns the ``resume_mode`` of the interaction so the caller can decide
    whether to also inject an ``[INTERACTION_RESPONSE]`` block into the agent.
    """
    result = await db.execute(
        select(GSageInteraction).where(
            GSageInteraction.id == interaction_id,
            GSageInteraction.org_id == org_id,
        )
    )
    interaction = result.scalar_one_or_none()
    if interaction is None:
        raise ValueError(f"Interaction {interaction_id} not found")

    if interaction.status != "waiting_input":
        raise ValueError(
            f"Interaction {interaction_id} already {interaction.status}"
        )

    # Persist
    interaction.status = "submitted"
    interaction.response_json = responses
    await db.commit()

    # Dispatch to the waiting tool (CONTINUE_TOOL) via Redis
    broker = RedisBroker(_get_redis())
    await broker.send_response(interaction_id, responses)

    log.info(
        "Interaction %s submitted — resume_mode=%s",
        interaction_id, interaction.resume_mode,
    )
    return interaction.resume_mode


async def cancel_interaction(
    interaction_id: uuid.UUID,
    org_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    """Validate ownership, mark cancelled, and notify the waiting tool."""
    result = await db.execute(
        select(GSageInteraction).where(
            GSageInteraction.id == interaction_id,
            GSageInteraction.org_id == org_id,
        )
    )
    interaction = result.scalar_one_or_none()
    if interaction is None:
        raise ValueError(f"Interaction {interaction_id} not found")

    if interaction.status != "waiting_input":
        raise ValueError(
            f"Interaction {interaction_id} already {interaction.status}"
        )

    interaction.status = "cancelled"
    await db.commit()

    broker = RedisBroker(_get_redis())
    await broker.send_cancellation(interaction_id)

    log.info("Interaction %s cancelled", interaction_id)


def build_interaction_response_block(
    interaction_id: uuid.UUID,
    responses: dict,
    context: Optional[dict] = None,
) -> str:
    """Build a ``[INTERACTION_RESPONSE]`` context block for agent injection.

    Used by REPLAN_AGENT flow: the responses are formatted as a structured
    block the agent can parse and use for replanning.
    """
    import json as _json

    lines = ["[INTERACTION_RESPONSE]"]
    lines.append(f"interaction_id: {interaction_id}")
    if context:
        lines.append(f"context: {_json.dumps(context, ensure_ascii=False)}")
    lines.append("responses:")
    for key, value in responses.items():
        lines.append(f"  {key}: {value}")
    lines.append("[/INTERACTION_RESPONSE]")
    return "\n".join(lines)
