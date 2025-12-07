"""Tests for burndown rate functionality.

Tests cover the burndown rate feature for AWS Bedrock style rate limiting
where output tokens are multiplied by a factor for TPM calculation.
"""

import time
from unittest.mock import AsyncMock

import pytest

from llmratelimiter import RateLimitConfig, RateLimiter


class TestBurndownRateConfig:
    """Configuration tests for burndown rate."""

    def test_default_burndown_rate_is_one(self) -> None:
        """Default burndown rate should be 1.0."""
        config = RateLimitConfig(tpm=100_000, rpm=100)
        assert config.burndown_rate == 1.0

    def test_custom_burndown_rate_via_config(self) -> None:
        """Custom burndown rate can be set via config."""
        config = RateLimitConfig(tpm=100_000, rpm=100, burndown_rate=5.0)
        assert config.burndown_rate == 5.0

    def test_burndown_rate_validation_negative(self) -> None:
        """Negative burndown rate should raise ValueError."""
        with pytest.raises(ValueError, match="burndown_rate must be >= 0"):
            RateLimitConfig(tpm=100_000, rpm=100, burndown_rate=-1.0)

    def test_burndown_rate_zero_is_valid(self) -> None:
        """Zero burndown rate should be valid (output tokens don't count)."""
        config = RateLimitConfig(tpm=100_000, rpm=100, burndown_rate=0.0)
        assert config.burndown_rate == 0.0


class TestBurndownRateLimiterInit:
    """Limiter initialization tests for burndown rate."""

    @pytest.mark.asyncio
    async def test_burndown_rate_via_constructor_kwarg(self) -> None:
        """Burndown rate can be set via constructor kwarg."""
        mock_redis = AsyncMock()
        limiter = RateLimiter(mock_redis, "claude-sonnet", tpm=100_000, rpm=100, burndown_rate=5.0)
        assert limiter._burndown_rate == 5.0

    @pytest.mark.asyncio
    async def test_burndown_rate_via_config(self) -> None:
        """Burndown rate can be set via config object."""
        mock_redis = AsyncMock()
        config = RateLimitConfig(tpm=100_000, rpm=100, burndown_rate=5.0)
        limiter = RateLimiter(mock_redis, "claude-sonnet", config)
        assert limiter._burndown_rate == 5.0

    @pytest.mark.asyncio
    async def test_default_burndown_rate_in_limiter(self) -> None:
        """Default burndown rate in limiter should be 1.0."""
        mock_redis = AsyncMock()
        limiter = RateLimiter(mock_redis, "gpt-4", tpm=100_000, rpm=100)
        assert limiter._burndown_rate == 1.0


class TestBurndownRateAcquire:
    """Acquire tests for burndown rate."""

    @pytest.mark.asyncio
    async def test_effective_combined_tokens_with_burndown_rate(self) -> None:
        """Effective combined tokens should be calculated with burndown rate."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        limiter = RateLimiter(mock_redis, "claude-sonnet", tpm=100_000, rpm=100, burndown_rate=5.0)

        # Call acquire with input and output tokens
        await limiter.acquire(input_tokens=3000, output_tokens=1000)

        # Check the effective_combined_tokens passed to Lua script
        # Args: [0]=script, [1]=1, [2]=key, [3]=input, [4]=output, [5-8]=limits,
        #       [9]=window, [10]=time, [11]=record_id, [12]=effective_combined
        # Expected: 3000 + (5.0 * 1000) = 8000
        call_args = mock_redis.eval.call_args
        effective_combined = call_args[0][12]
        assert effective_combined == 8000.0

    @pytest.mark.asyncio
    async def test_tokens_only_bypasses_burndown_rate(self) -> None:
        """Using tokens= parameter should bypass burndown rate (assume already calculated)."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        limiter = RateLimiter(mock_redis, "claude-sonnet", tpm=100_000, rpm=100, burndown_rate=5.0)

        # Call acquire with tokens (assume caller already applied burndown)
        await limiter.acquire(tokens=8000)  # e.g., 3000 input + 5*1000 output

        # Effective combined should equal the exact tokens value (no burndown applied)
        call_args = mock_redis.eval.call_args
        effective_combined = call_args[0][12]
        assert effective_combined == 8000.0  # Used directly, not modified

    @pytest.mark.asyncio
    async def test_default_burndown_rate_calculation(self) -> None:
        """Default burndown rate (1.0) should result in input + output."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        limiter = RateLimiter(
            mock_redis,
            "gpt-4",
            tpm=100_000,
            rpm=100,  # default burndown_rate=1.0
        )

        await limiter.acquire(input_tokens=3000, output_tokens=1000)

        # Effective combined should be input + output
        # Expected: 3000 + (1.0 * 1000) = 4000
        call_args = mock_redis.eval.call_args
        effective_combined = call_args[0][12]
        assert effective_combined == 4000.0

    @pytest.mark.asyncio
    async def test_zero_burndown_rate(self) -> None:
        """Zero burndown rate should mean output tokens don't count toward TPM."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        limiter = RateLimiter(mock_redis, "test-model", tpm=100_000, rpm=100, burndown_rate=0.0)

        await limiter.acquire(input_tokens=3000, output_tokens=10000)

        # Effective combined should be input only
        # Expected: 3000 + (0.0 * 10000) = 3000
        call_args = mock_redis.eval.call_args
        effective_combined = call_args[0][12]
        assert effective_combined == 3000.0


class TestBurndownRateWithSplitMode:
    """Tests for burndown rate with split TPM mode."""

    @pytest.mark.asyncio
    async def test_split_mode_stores_raw_tokens(self) -> None:
        """In split mode, raw input/output tokens should be stored."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        limiter = RateLimiter(
            mock_redis,
            "gemini",
            input_tpm=100_000,
            output_tpm=20_000,
            rpm=100,
            burndown_rate=5.0,  # Should not affect split limits
        )

        await limiter.acquire(input_tokens=3000, output_tokens=1000)

        call_args = mock_redis.eval.call_args
        # Raw input/output tokens should be passed
        input_tokens_arg = call_args[0][3]  # ARGV[1]
        output_tokens_arg = call_args[0][4]  # ARGV[2]
        assert input_tokens_arg == 3000
        assert output_tokens_arg == 1000


class TestBurndownRateMixedMode:
    """Tests for burndown rate with mixed mode (combined + split)."""

    @pytest.mark.asyncio
    async def test_mixed_mode_applies_burndown_to_combined_only(self) -> None:
        """In mixed mode, burndown rate applies to combined TPM only."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        limiter = RateLimiter(
            mock_redis,
            "test-model",
            tpm=100_000,  # Combined limit
            input_tpm=80_000,  # Split limits
            output_tpm=20_000,
            rpm=100,
            burndown_rate=5.0,
        )

        await limiter.acquire(input_tokens=3000, output_tokens=1000)

        call_args = mock_redis.eval.call_args
        # Raw tokens for split limits
        input_tokens_arg = call_args[0][3]
        output_tokens_arg = call_args[0][4]
        assert input_tokens_arg == 3000
        assert output_tokens_arg == 1000

        # Effective combined with burndown rate
        effective_combined = call_args[0][12]
        assert effective_combined == 8000.0  # 3000 + (5.0 * 1000)
