"""gSage AI — RedisBroker: V1 InteractionBroker implementation.

.. attention::

   **Temporary limitation (V1):** This implementation uses BRPOP (blocking
   list pop) to wait for user responses.  Consequences:

   * The MCP HTTP call stays open for the entire interaction duration.
   * If the MCP server restarts, in-flight interactions are lost.
   * Tools cannot survive process restarts mid-interaction.

   Future versions will replace this with a durable-execution backend
   (workflow engine, persistent timers, replay on restart) without
   changing the :class:`InteractionBroker` interface or the Tool API.
"""

from __future__ import annotations

import json
import uuid

import redis.asyncio as redis

from src.shared.interaction.broker import InteractionBroker
from src.shared.interaction.exceptions import InteractionCancelled, InteractionTimeout

# Sentinel value pushed to the Redis list to signal cancellation.
_CANCELLED_SENTINEL = "__interaction_cancelled__"


class RedisBroker(InteractionBroker):
    """V1 broker using Redis pub/sub + BRPOP list queues."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client

    # ── publish_request ─────────────────────────────────────────────────

    async def publish_request(
        self,
        gsage_session_id: uuid.UUID,
        payload: dict,
    ) -> None:
        channel = f"interaction:conv:{gsage_session_id}"
        await self._redis.publish(channel, json.dumps(payload))

    # ── wait_for_response (BLOCKING) ────────────────────────────────────

    async def wait_for_response(
        self,
        interaction_id: uuid.UUID,
        timeout_seconds: int,
    ) -> dict:
        key = f"interaction:response:{interaction_id}"
        result = await self._redis.brpop([key], timeout=timeout_seconds)  # type: ignore[arg-type]

        if result is None:
            raise InteractionTimeout(
                f"Interaction {interaction_id} timed out after {timeout_seconds}s"
            )

        _, raw = result
        data: dict = json.loads(raw)

        if data.get(_CANCELLED_SENTINEL):
            raise InteractionCancelled(
                f"Interaction {interaction_id} was cancelled by the user"
            )

        return data

    # ── send_response (called by backend API) ───────────────────────────

    async def send_response(
        self,
        interaction_id: uuid.UUID,
        response: dict,
    ) -> None:
        key = f"interaction:response:{interaction_id}"
        await self._redis.lpush(key, json.dumps(response))  # type: ignore[await]
        # Expire so abandoned keys don't accumulate
        await self._redis.expire(key, 3600)  # type: ignore[await]

    # ── send_cancellation (called by backend API) ───────────────────────

    async def send_cancellation(
        self,
        interaction_id: uuid.UUID,
    ) -> None:
        key = f"interaction:response:{interaction_id}"
        await self._redis.lpush(key, json.dumps({_CANCELLED_SENTINEL: True}))  # type: ignore[await]
        await self._redis.expire(key, 60)  # type: ignore[await]
