"""gSage AI — InteractionBroker abstract interface."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod


class InteractionBroker(ABC):
    """Abstract interface for cross-process interaction signaling.

    **V1 implementation:** :class:`RedisBroker` (pub/sub + BRPOP).

    **Future:** Kafka, RabbitMQ, Postgres LISTEN/NOTIFY, gRPC stream,
    durable-execution workflow engine, …

    The rest of the system MUST only depend on this interface,
    never on Redis (or any specific backend) directly.
    """

    @abstractmethod
    async def publish_request(
        self,
        gsage_session_id: uuid.UUID,
        payload: dict,
    ) -> None:
        """Publish an ``interaction.requested`` event to the conversation SSE channel."""
        ...

    @abstractmethod
    async def wait_for_response(
        self,
        interaction_id: uuid.UUID,
        timeout_seconds: int,
    ) -> dict:
        """Block until the user submits (or timeout/cancel).

        Returns:
            The response dict on success.

        Raises:
            InteractionTimeout: No response within *timeout_seconds*.
            InteractionCancelled: User explicitly cancelled.
        """
        ...

    @abstractmethod
    async def send_response(
        self,
        interaction_id: uuid.UUID,
        response: dict,
    ) -> None:
        """Push a response to the waiting tool (called by the backend API)."""
        ...

    @abstractmethod
    async def send_cancellation(
        self,
        interaction_id: uuid.UUID,
    ) -> None:
        """Push a cancellation sentinel to unblock the waiting tool."""
        ...
