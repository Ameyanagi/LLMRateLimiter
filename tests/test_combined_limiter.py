"""Tests for RateLimiter in combined mode.

Tests cover combined TPM+RPM limiting (OpenAI/Anthropic style).
"""

import time
from unittest.mock import AsyncMock

import pytest

from llmratelimiter import RateLimitConfig, RateLimiter
from llmratelimiter.models import AcquireResult, RateLimitStatus


class TestCombinedModeBasic:
    """Basic functionality tests for combined mode."""

    @pytest.mark.asyncio
    async def test_acquire_under_limits_returns_immediately(self) -> None:
        """When under limits, acquire should return immediately with no wait."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        config = RateLimitConfig(tpm=100_000, rpm=100)
        limiter = RateLimiter(mock_redis, "gpt-4", config)

        result = await limiter.acquire(tokens=5000)

        assert isinstance(result, AcquireResult)
        assert result.wait_time == 0.0
        assert result.queue_position == 0
        assert result.record_id == "test-id"

    @pytest.mark.asyncio
    async def test_acquire_at_capacity_waits(self) -> None:
        """When at capacity, acquire should wait for the calculated time."""
        mock_redis = AsyncMock()
        current_time = time.time()
        wait_time = 0.1  # 100ms wait for testing
        mock_redis.eval = AsyncMock(return_value=[current_time + wait_time, 1, "test-id", wait_time])

        config = RateLimitConfig(tpm=10_000, rpm=2)
        limiter = RateLimiter(mock_redis, "gpt-4", config)

        start = time.time()
        result = await limiter.acquire(tokens=5000)
        elapsed = time.time() - start

        assert result.wait_time == pytest.approx(wait_time, abs=0.05)
        assert elapsed >= wait_time * 0.9  # Allow some tolerance

    @pytest.mark.asyncio
    async def test_acquire_returns_queue_position(self) -> None:
        """Acquire should return correct queue position."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time + 1.0, 3, "test-id", 1.0])

        config = RateLimitConfig(tpm=10_000, rpm=2)
        limiter = RateLimiter(mock_redis, "gpt-4", config)
        limiter._should_wait = False

        result = await limiter.acquire(tokens=5000)

        assert result.queue_position == 3


class TestCombinedModeConfig:
    """Configuration tests for combined mode."""

    @pytest.mark.asyncio
    async def test_burst_multiplier_applied(self) -> None:
        """Burst multiplier should increase effective limits."""
        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(return_value=[time.time(), 0, "test-id", 0.0])

        config = RateLimitConfig(tpm=100_000, rpm=100, burst_multiplier=1.5)
        limiter = RateLimiter(mock_redis, "gpt-4", config)

        assert limiter.tpm_limit == 150_000
        assert limiter.rpm_limit == 150

    @pytest.mark.asyncio
    async def test_custom_window_seconds(self) -> None:
        """Custom window_seconds should be used."""
        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(return_value=[time.time(), 0, "test-id", 0.0])

        config = RateLimitConfig(tpm=100_000, rpm=100, window_seconds=120)
        limiter = RateLimiter(mock_redis, "gpt-4", config)

        assert limiter.window_seconds == 120

    @pytest.mark.asyncio
    async def test_is_split_mode_false_for_combined(self) -> None:
        """is_split_mode should be False for combined config."""
        mock_redis = AsyncMock()
        config = RateLimitConfig(tpm=100_000, rpm=100)
        limiter = RateLimiter(mock_redis, "gpt-4", config)

        assert not limiter.is_split_mode


class TestCombinedModeGracefulDegradation:
    """Graceful degradation tests for combined mode."""

    @pytest.mark.asyncio
    async def test_redis_error_allows_request(self) -> None:
        """On Redis error, request should be allowed (graceful degradation)."""
        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(side_effect=Exception("Redis connection failed"))

        config = RateLimitConfig(tpm=100_000, rpm=100)
        limiter = RateLimiter(mock_redis, "gpt-4", config)

        result = await limiter.acquire(tokens=5000)

        assert result.wait_time == 0.0
        assert result.record_id is not None


class TestCombinedModeStatus:
    """Status retrieval tests for combined mode."""

    @pytest.mark.asyncio
    async def test_get_status_returns_correct_info(self) -> None:
        """get_status should return current usage information."""
        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(return_value=[50000, 0, 10, 2])

        config = RateLimitConfig(tpm=100_000, rpm=100)
        limiter = RateLimiter(mock_redis, "gpt-4", config)

        status = await limiter.get_status()

        assert isinstance(status, RateLimitStatus)
        assert status.model == "gpt-4"
        assert status.tokens_limit == 100_000
        assert status.tokens_used == 50000
        assert status.requests_limit == 100
        assert status.requests_used == 10
        assert status.queue_depth == 2


class TestCombinedModeLuaScript:
    """Tests for Lua script arguments in combined mode."""

    @pytest.mark.asyncio
    async def test_lua_script_receives_correct_arguments(self) -> None:
        """Lua script should receive correct arguments."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        config = RateLimitConfig(tpm=100_000, rpm=100, window_seconds=60)
        limiter = RateLimiter(mock_redis, "gpt-4", config)

        await limiter.acquire(tokens=5000)

        mock_redis.eval.assert_called_once()
        call_args = mock_redis.eval.call_args

        # First arg is the script, second is number of keys
        assert call_args[0][1] == 1  # One key

        # Check that key is correct
        assert "rate_limit:gpt-4:consumption" in call_args[0][2]
