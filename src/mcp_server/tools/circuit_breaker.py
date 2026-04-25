"""gSage AI — Redis-backed circuit breaker for tools."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from enum import Enum

import redis.asyncio as redis

logger = logging.getLogger(__name__)

# Circuit breaker thresholds (per PROMPT.md Phase 4)
CIRCUIT_FAILURE_THRESHOLD = 5       # consecutive failures before OPEN
CIRCUIT_OPEN_TIMEOUT_SECONDS = 60   # time in OPEN state before HALF_OPEN probe


class CircuitState(str, Enum):
    """Circuit breaker states."""

    CLOSED = "CLOSED"        # Normal — execute requests
    OPEN = "OPEN"            # Failing — reject requests immediately
    HALF_OPEN = "HALF_OPEN"  # Probing — allow one request through


class CircuitBreaker:
    """
    Redis-backed circuit breaker for tool execution.

    Redis key: ``circuit:{tool_name}``
    Value: JSON with state, failure_count, last_failure_at, opened_at.

    State transitions:
        CLOSED → OPEN  : on CIRCUIT_FAILURE_THRESHOLD consecutive failures
        OPEN → HALF_OPEN : after CIRCUIT_OPEN_TIMEOUT_SECONDS
        HALF_OPEN → CLOSED : on successful probe
        HALF_OPEN → OPEN   : on failed probe
    """

    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client

    def _key(self, tool_name: str) -> str:
        return f"circuit:{tool_name}"

    async def _load(self, tool_name: str) -> dict:
        data = await self.redis.get(self._key(tool_name))
        if data is None:
            return {
                "state": CircuitState.CLOSED,
                "failure_count": 0,
                "last_failure_at": None,
                "opened_at": None,
            }
        return json.loads(data)

    async def _save(self, tool_name: str, state: dict) -> None:
        await self.redis.set(
            self._key(tool_name),
            json.dumps(state),
            ex=3600,  # TTL: 1 hour — cleared if tool is healthy for 1h
        )

    async def get_state(self, tool_name: str) -> CircuitState:
        """Get current circuit state, auto-transitioning OPEN → HALF_OPEN if timeout elapsed."""
        state = await self._load(tool_name)
        circuit_state = CircuitState(state["state"])

        # Auto-transition OPEN → HALF_OPEN after timeout
        if circuit_state == CircuitState.OPEN and state.get("opened_at"):
            opened_at = datetime.fromisoformat(state["opened_at"])
            elapsed = (datetime.now(timezone.utc) - opened_at).total_seconds()
            if elapsed >= CIRCUIT_OPEN_TIMEOUT_SECONDS:
                state["state"] = CircuitState.HALF_OPEN
                await self._save(tool_name, state)
                logger.info("Circuit breaker HALF_OPEN for tool: %s", tool_name)
                return CircuitState.HALF_OPEN

        return circuit_state

    async def is_available(self, tool_name: str) -> bool:
        """Return True if circuit allows execution (CLOSED or HALF_OPEN)."""
        circuit_state = await self.get_state(tool_name)
        return circuit_state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    async def record_success(self, tool_name: str) -> None:
        """Record successful execution. Resets circuit on HALF_OPEN probe success."""
        state = await self._load(tool_name)
        circuit_state = CircuitState(state["state"])

        if circuit_state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
            logger.info("Circuit breaker CLOSED for tool: %s (probe succeeded)", tool_name)

        state["state"] = CircuitState.CLOSED
        state["failure_count"] = 0
        state["last_failure_at"] = None
        state["opened_at"] = None
        await self._save(tool_name, state)

    async def record_failure(self, tool_name: str) -> CircuitState:
        """
        Record failed execution. Opens circuit after threshold.

        Returns:
            New circuit state after recording failure.
        """
        state = await self._load(tool_name)
        now = datetime.now(timezone.utc).isoformat()

        state["failure_count"] = state.get("failure_count", 0) + 1
        state["last_failure_at"] = now

        if state["failure_count"] >= CIRCUIT_FAILURE_THRESHOLD:
            if CircuitState(state["state"]) != CircuitState.OPEN:
                logger.warning(
                    "Circuit breaker OPEN for tool: %s (failures: %d)",
                    tool_name,
                    state["failure_count"],
                )
            state["state"] = CircuitState.OPEN
            state["opened_at"] = state.get("opened_at") or now  # preserve original open time

        await self._save(tool_name, state)
        return CircuitState(state["state"])
