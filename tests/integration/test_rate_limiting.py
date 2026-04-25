"""Sprint 5.3 — Rate limiting unit tests.

Tests the :func:`check_rate_limit` FastAPI dependency in isolation using a
mocked Redis connection.  No real Redis or database is required.

Strategy
--------
* Patch ``src.backend_api.app.api.middleware.rate_limit._redis`` with a
  ``MagicMock`` whose ``pipeline().execute()`` returns controlled values.
* Call ``check_rate_limit`` directly (bypassing HTTP) to assert 429 behaviour.
* Verify ``X-RateLimit-*`` headers are populated in ``request.state``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.backend_api.app.api.middleware.rate_limit import check_rate_limit
from src.backend_api.app.core.tenant import TenantContext, permissions_for_role
from src.shared.config.settings import get_settings
from tests.conftest import ORG_A, ORG_B, USER_A, USER_B


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tenant(org_id=ORG_A, user_id=USER_A, rpm_override=None) -> TenantContext:
    return TenantContext(
        user_id=user_id,
        org_id=org_id,
        org_role="member",
        permissions=permissions_for_role("member"),
        rate_limit_per_minute=rpm_override,
    )


def _make_request_state():
    """Return a simple object that supports attribute assignment (like Starlette's State)."""
    class _State:
        pass
    req = MagicMock()
    req.state = _State()
    return req


def _mock_redis_pipeline(org_count: int, user_count: int) -> MagicMock:
    """Return a mock Redis client whose pipeline returns *org_count* and *user_count*."""
    mock_pipe = MagicMock()
    mock_pipe.incr = MagicMock()
    mock_pipe.execute = AsyncMock(return_value=[org_count, user_count])

    mock_redis = MagicMock()
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)
    mock_redis.expire = AsyncMock(return_value=True)
    return mock_redis


# ---------------------------------------------------------------------------
# Core tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_under_limit_passes():
    """Request under both org and user limits passes without raising."""
    settings = get_settings()
    mock_redis = _mock_redis_pipeline(
        org_count=1, user_count=1
    )
    tc = _make_tenant()
    request = _make_request_state()

    with patch("src.backend_api.app.api.middleware.rate_limit._redis", mock_redis):
        await check_rate_limit(request=request, tc=tc)

    # Headers should be set on request.state
    assert request.state.rl_limit == settings.rate_limit_default_rpm
    assert request.state.rl_remaining == settings.rate_limit_default_rpm - 1
    assert request.state.rl_reset > 0


@pytest.mark.unit
async def test_org_limit_exceeded_raises_429():
    """Raises HTTP 429 when the org counter exceeds rate_limit_default_rpm."""
    settings = get_settings()
    over_limit = settings.rate_limit_default_rpm + 1
    mock_redis = _mock_redis_pipeline(org_count=over_limit, user_count=1)
    tc = _make_tenant()
    request = _make_request_state()

    with patch("src.backend_api.app.api.middleware.rate_limit._redis", mock_redis):
        with pytest.raises(HTTPException) as exc_info:
            await check_rate_limit(request=request, tc=tc)

    assert exc_info.value.status_code == 429
    assert "organization" in exc_info.value.detail.lower()
    # Headers must be present in the exception
    headers = exc_info.value.headers or {}
    assert headers.get("X-RateLimit-Limit") == str(settings.rate_limit_default_rpm)
    assert headers.get("X-RateLimit-Remaining") == "0"
    assert "Retry-After" in headers


@pytest.mark.unit
async def test_user_limit_exceeded_raises_429():
    """Raises HTTP 429 when the user counter exceeds rate_limit_user_rpm."""
    settings = get_settings()
    user_over = settings.rate_limit_user_rpm + 1
    mock_redis = _mock_redis_pipeline(org_count=1, user_count=user_over)
    tc = _make_tenant()
    request = _make_request_state()

    with patch("src.backend_api.app.api.middleware.rate_limit._redis", mock_redis):
        with pytest.raises(HTTPException) as exc_info:
            await check_rate_limit(request=request, tc=tc)

    assert exc_info.value.status_code == 429
    assert "user" in exc_info.value.detail.lower()


@pytest.mark.unit
async def test_api_key_override_uses_custom_rpm():
    """When rate_limit_per_minute is set on the TenantContext, it overrides the global limit."""
    custom_rpm = 5
    tc = _make_tenant(rpm_override=custom_rpm)
    request = _make_request_state()

    # org_count = 6 > custom_rpm(5) → should be rejected
    mock_redis = _mock_redis_pipeline(org_count=6, user_count=1)

    with patch("src.backend_api.app.api.middleware.rate_limit._redis", mock_redis):
        with pytest.raises(HTTPException) as exc_info:
            await check_rate_limit(request=request, tc=tc)

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers is not None
    assert exc_info.value.headers.get("X-RateLimit-Limit") == str(custom_rpm)


@pytest.mark.unit
async def test_api_key_override_passes_under_custom_rpm():
    """Custom RPM limit allows requests under its threshold even if global would be hit."""
    settings = get_settings()
    over_global = settings.rate_limit_default_rpm + 10  # over global but under custom
    custom_rpm = over_global + 100  # custom limit higher than the org count
    tc = _make_tenant(rpm_override=custom_rpm)
    request = _make_request_state()

    # org_count = over_global < custom_rpm → should pass
    mock_redis = _mock_redis_pipeline(org_count=over_global, user_count=1)

    with patch("src.backend_api.app.api.middleware.rate_limit._redis", mock_redis):
        await check_rate_limit(request=request, tc=tc)  # no exception

    assert request.state.rl_limit == custom_rpm


@pytest.mark.unit
async def test_redis_error_fails_open():
    """When Redis raises an exception, the request is allowed through (fail-open)."""
    mock_pipe = MagicMock()
    mock_pipe.incr = MagicMock()
    mock_pipe.expire = MagicMock()
    mock_pipe.execute = AsyncMock(side_effect=ConnectionError("Redis down"))

    mock_redis = MagicMock()
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)

    tc = _make_tenant()
    request = _make_request_state()

    with patch("src.backend_api.app.api.middleware.rate_limit._redis", mock_redis):
        # Should NOT raise — fail open
        await check_rate_limit(request=request, tc=tc)


@pytest.mark.unit
async def test_rate_limiting_disabled_skips_redis():
    """When rate_limit_enabled=False, Redis is never called."""
    tc = _make_tenant()
    request = _make_request_state()

    mock_redis = MagicMock()

    with patch("src.backend_api.app.api.middleware.rate_limit._redis", mock_redis):
        with patch(
            "src.backend_api.app.api.middleware.rate_limit.get_settings",
            return_value=MagicMock(rate_limit_enabled=False),
        ):
            await check_rate_limit(request=request, tc=tc)

    mock_redis.pipeline.assert_not_called()


@pytest.mark.unit
async def test_different_orgs_use_separate_redis_keys():
    """Org A and Org B counters are keyed separately in Redis."""
    calls: list[str] = []

    def _capturing_pipeline(transaction=False):
        pipe = MagicMock()

        def _incr(key):
            calls.append(key)
            return pipe

        pipe.incr = _incr
        pipe.execute = AsyncMock(return_value=[1, 1])
        return pipe

    mock_redis = MagicMock()
    mock_redis.pipeline = _capturing_pipeline
    mock_redis.expire = AsyncMock(return_value=True)

    tc_a = _make_tenant(org_id=ORG_A, user_id=USER_A)
    tc_b = _make_tenant(org_id=ORG_B, user_id=USER_B)

    with patch("src.backend_api.app.api.middleware.rate_limit._redis", mock_redis):
        await check_rate_limit(request=_make_request_state(), tc=tc_a)
        await check_rate_limit(request=_make_request_state(), tc=tc_b)

    org_a_key = f"rl:org:{ORG_A}"
    org_b_key = f"rl:org:{ORG_B}"
    user_a_key = f"rl:user:{ORG_A}:{USER_A}"
    user_b_key = f"rl:user:{ORG_B}:{USER_B}"

    assert org_a_key in calls, f"Expected {org_a_key!r} in {calls}"
    assert org_b_key in calls, f"Expected {org_b_key!r} in {calls}"
    assert user_a_key in calls, f"Expected {user_a_key!r} in {calls}"
    assert user_b_key in calls, f"Expected {user_b_key!r} in {calls}"

    # Keys must be different — no cross-contamination
    assert org_a_key != org_b_key
    assert user_a_key != user_b_key
