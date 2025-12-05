"""Tests for RateLimiter in split mode.

Tests cover split input/output TPM limiting (GCP Vertex AI style).
"""

import time
from unittest.mock import AsyncMock

import pytest

from llmratelimiter import RateLimitConfig, RateLimiter
from llmratelimiter.models import AcquireResult, RateLimitStatus


class TestSplitModeBasic:
    """Basic functionality tests for split mode."""

    @pytest.mark.asyncio
    async def test_acquire_under_limits_returns_immediately(self) -> None:
        """When under all limits, acquire should return immediately."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        config = RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)
        limiter = RateLimiter(mock_redis, "gemini-1.5-pro", config)

        result = await limiter.acquire(input_tokens=5000, output_tokens=2000)

        assert isinstance(result, AcquireResult)
        assert result.wait_time == 0.0
        assert result.queue_position == 0

    @pytest.mark.asyncio
    async def test_acquire_with_default_output_tokens(self) -> None:
        """Should use default output_tokens if not specified."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        config = RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)
        limiter = RateLimiter(mock_redis, "gemini-1.5-pro", config)

        # Should not raise - output_tokens has a default
        result = await limiter.acquire(input_tokens=5000)

        assert result is not None


class TestSplitModeConfig:
    """Configuration tests for split mode."""

    @pytest.mark.asyncio
    async def test_burst_multiplier_applied_to_all_limits(self) -> None:
        """Burst multiplier should increase all limits."""
        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(return_value=[time.time(), 0, "test-id", 0.0])

        config = RateLimitConfig(
            input_tpm=4_000_000,
            output_tpm=128_000,
            rpm=360,
            burst_multiplier=1.5,
        )
        limiter = RateLimiter(mock_redis, "gemini-1.5-pro", config)

        assert limiter.input_tpm_limit == 6_000_000
        assert limiter.output_tpm_limit == 192_000
        assert limiter.rpm_limit == 540

    @pytest.mark.asyncio
    async def test_is_split_mode_true_for_split(self) -> None:
        """is_split_mode should be True for split config."""
        mock_redis = AsyncMock()
        config = RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)
        limiter = RateLimiter(mock_redis, "gemini", config)

        assert limiter.is_split_mode


class TestSplitModeAdjust:
    """Tests for the adjust() method."""

    @pytest.mark.asyncio
    async def test_adjust_updates_output_tokens(self) -> None:
        """adjust() should update the output tokens for a record."""
        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(return_value=[1, "updated"])

        config = RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)
        limiter = RateLimiter(mock_redis, "gemini-1.5-pro", config)

        # Should not raise
        await limiter.adjust("test-record-id", actual_output=1500)

        # Verify adjust script was called
        assert mock_redis.eval.called

    @pytest.mark.asyncio
    async def test_adjust_handles_not_found(self) -> None:
        """adjust() should handle record not found gracefully."""
        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(return_value=[0, "not_found"])

        config = RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)
        limiter = RateLimiter(mock_redis, "gemini-1.5-pro", config)

        # Should not raise even if record not found
        await limiter.adjust("nonexistent-id", actual_output=1500)

    @pytest.mark.asyncio
    async def test_adjust_works_in_combined_mode(self) -> None:
        """adjust() should work in combined mode too."""
        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(return_value=[1, "updated"])

        config = RateLimitConfig(tpm=100_000, rpm=100)
        limiter = RateLimiter(mock_redis, "gpt-4", config)

        # Should call Redis - adjust is useful for tracking actual usage
        await limiter.adjust("test-record-id", actual_output=1500)

        # eval should have been called for adjust
        mock_redis.eval.assert_called_once()


class TestSplitModeStatus:
    """Status retrieval tests for split mode."""

    @pytest.mark.asyncio
    async def test_get_status_returns_split_info(self) -> None:
        """get_status should return input and output token usage."""
        mock_redis = AsyncMock()
        # Mock status script result: [total_tokens, total_output, total_requests, queue_depth]
        mock_redis.eval = AsyncMock(return_value=[10000, 5000, 5, 2])

        config = RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)
        limiter = RateLimiter(mock_redis, "gemini-1.5-pro", config)

        status = await limiter.get_status()

        assert isinstance(status, RateLimitStatus)
        assert status.model == "gemini-1.5-pro"
        assert status.input_tokens_used == 10000
        assert status.output_tokens_used == 5000
        assert status.input_tokens_limit == 4_000_000
        assert status.output_tokens_limit == 128_000
        assert status.requests_used == 5
        assert status.queue_depth == 2


class TestSplitModeGracefulDegradation:
    """Graceful degradation tests for split mode."""

    @pytest.mark.asyncio
    async def test_redis_error_allows_request(self) -> None:
        """On Redis error, request should be allowed."""
        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(side_effect=Exception("Redis connection failed"))

        config = RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)
        limiter = RateLimiter(mock_redis, "gemini-1.5-pro", config)

        result = await limiter.acquire(input_tokens=5000, output_tokens=2000)

        assert result.wait_time == 0.0
        assert result.record_id is not None


class TestSplitModeLuaScript:
    """Tests for Lua script arguments in split mode."""

    @pytest.mark.asyncio
    async def test_lua_script_receives_both_token_types(self) -> None:
        """Lua script should receive input and output tokens."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        config = RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)
        limiter = RateLimiter(mock_redis, "gemini-1.5-pro", config)

        await limiter.acquire(input_tokens=5000, output_tokens=2000)

        mock_redis.eval.assert_called_once()
        call_args = mock_redis.eval.call_args

        # Verify both token types are passed
        # Args order: script, num_keys, key, tokens, output_tokens, ...
        args = call_args[0]
        assert args[3] == 5000  # input tokens
        assert args[4] == 2000  # output tokens
