"""Integration tests for RedisBroker (pub/sub + BRPOP flow).

Requires a running Redis instance (the dev Docker Compose stack).
Skip with ``pytest -m "not integration"`` when Redis is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import redis.asyncio as redis

from src.shared.interaction.broker_redis import RedisBroker
from src.shared.interaction.exceptions import InteractionCancelled, InteractionTimeout
from src.shared.config.settings import get_settings

pytestmark = pytest.mark.integration


# ── Helpers ──────────────────────────────────────────────────────────────


def _client() -> redis.Redis:
    settings = get_settings()
    return redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )


async def _flush_test_keys(client: redis.Redis, *keys: str) -> None:
    if keys:
        await client.delete(*keys)


# ── publish_request ──────────────────────────────────────────────────────


class TestPublishRequest:
    async def test_publishes_to_correct_channel(self) -> None:
        client = _client()
        broker = RedisBroker(client)
        session_id = uuid.uuid4()
        payload = {"interaction_id": str(uuid.uuid4()), "type": "form"}

        # Subscribe before publishing
        pubsub = client.pubsub()
        channel = f"interaction:conv:{session_id}"
        await pubsub.subscribe(channel)

        try:
            await broker.publish_request(session_id, payload)

            # Receive the message
            msg = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=2.0
            )
            assert msg is not None
            data = json.loads(msg["data"])
            assert data == payload
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()


# ── send_response + wait_for_response (CONTINUE_TOOL flow) ───────────────


class TestSendAndWait:
    async def test_response_roundtrip(self) -> None:
        client = _client()
        broker = RedisBroker(client)
        interaction_id = uuid.uuid4()
        expected = {"nome": "João", "idade": 30}

        async def _wait_then_respond() -> None:
            """Simulate backend API receiving a submission after a short delay."""
            await asyncio.sleep(0.1)
            await broker.send_response(interaction_id, expected)

        # Start the waiter in the background
        task = asyncio.create_task(_wait_then_respond())

        try:
            received = await broker.wait_for_response(interaction_id, timeout_seconds=5)
            assert received == expected
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # Clean up response key
            await _flush_test_keys(
                client,
                f"interaction:response:{interaction_id}",
            )

    async def test_wait_for_response_timeout(self) -> None:
        client = _client()
        broker = RedisBroker(client)
        interaction_id = uuid.uuid4()

        with pytest.raises(InteractionTimeout):
            await broker.wait_for_response(interaction_id, timeout_seconds=1)


# ── send_cancellation ────────────────────────────────────────────────────


class TestCancellation:
    async def test_cancellation_unblocks_waiting_tool(self) -> None:
        client = _client()
        broker = RedisBroker(client)
        interaction_id = uuid.uuid4()

        async def _wait_then_cancel() -> None:
            await asyncio.sleep(0.1)
            await broker.send_cancellation(interaction_id)

        task = asyncio.create_task(_wait_then_cancel())

        try:
            with pytest.raises(InteractionCancelled):
                await broker.wait_for_response(interaction_id, timeout_seconds=5)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await _flush_test_keys(
                client,
                f"interaction:response:{interaction_id}",
            )


# ── Key expiry ───────────────────────────────────────────────────────────


class TestKeyExpiry:
    async def test_response_key_has_expiry(self) -> None:
        client = _client()
        broker = RedisBroker(client)
        interaction_id = uuid.uuid4()

        await broker.send_response(interaction_id, {"ok": True})

        key = f"interaction:response:{interaction_id}"
        ttl = await client.ttl(key)
        assert ttl > 0  # Key should have an expiry set
        assert ttl <= 3600  # Default 1 hour

        await _flush_test_keys(client, key)

    async def test_cancellation_key_has_expiry(self) -> None:
        client = _client()
        broker = RedisBroker(client)
        interaction_id = uuid.uuid4()

        await broker.send_cancellation(interaction_id)

        key = f"interaction:response:{interaction_id}"
        ttl = await client.ttl(key)
        assert ttl > 0
        assert ttl <= 60  # Cancellation keys expire faster

        await _flush_test_keys(client, key)
