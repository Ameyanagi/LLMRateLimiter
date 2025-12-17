"""Tests for RPS smoothing functionality.

Tests cover the RPS smoothing feature for Azure OpenAI style burst prevention
where requests are spaced to prevent sub-second rate limit violations.
"""

import time
from unittest.mock import AsyncMock

import pytest

from llmratelimiter import RateLimitConfig, RateLimiter


class TestRPSSmoothingConfig:
    """Configuration tests for RPS smoothing."""

    def test_default_smoothing_enabled(self) -> None:
        """Default smoothing should be enabled."""
        config = RateLimitConfig(tpm=100_000, rpm=100)
        assert config.smooth_requests is True
        assert config.rps == 0
        assert config.smoothing_interval == 1.0
        assert config.is_smoothing_enabled is True
        assert config.effective_rps == 100 / 60.0  # Auto-calculated from RPM

    def test_enable_smoothing_auto_calculate_rps(self) -> None:
        """Enabling smooth_requests should auto-calculate RPS from RPM."""
        config = RateLimitConfig(tpm=100_000, rpm=600, smooth_requests=True)
        assert config.is_smoothing_enabled is True
        assert config.effective_rps == 10.0  # 600 / 60

    def test_auto_enable_when_rps_set(self) -> None:
        """Setting rps > 0 should auto-enable smoothing."""
        config = RateLimitConfig(tpm=100_000, rpm=600, rps=10)
        assert config.is_smoothing_enabled is True
        assert config.effective_rps == 10.0

    def test_explicit_rps_overrides_auto_calculation(self) -> None:
        """Explicit rps should override auto-calculation from RPM."""
        config = RateLimitConfig(tpm=100_000, rpm=600, smooth_requests=True, rps=8)
        assert config.is_smoothing_enabled is True
        assert config.effective_rps == 8.0  # Uses explicit, not 600/60=10

    def test_custom_smoothing_interval(self) -> None:
        """Custom smoothing interval should be configurable."""
        config = RateLimitConfig(tpm=100_000, rpm=600, smooth_requests=True, smoothing_interval=10.0)
        assert config.smoothing_interval == 10.0

    def test_rps_validation_negative(self) -> None:
        """Negative rps should raise ValueError."""
        with pytest.raises(ValueError, match="rps must be >= 0"):
            RateLimitConfig(tpm=100_000, rpm=100, rps=-1)

    def test_smoothing_interval_validation_zero(self) -> None:
        """Zero smoothing interval should raise ValueError."""
        with pytest.raises(ValueError, match="smoothing_interval must be > 0"):
            RateLimitConfig(tpm=100_000, rpm=100, smoothing_interval=0)

    def test_smoothing_interval_validation_negative(self) -> None:
        """Negative smoothing interval should raise ValueError."""
        with pytest.raises(ValueError, match="smoothing_interval must be > 0"):
            RateLimitConfig(tpm=100_000, rpm=100, smoothing_interval=-1.0)

    def test_no_auto_calculate_when_rpm_zero(self) -> None:
        """Auto-calculation should not happen when RPM is 0."""
        config = RateLimitConfig(tpm=100_000, rpm=0, smooth_requests=True)
        assert config.is_smoothing_enabled is True
        assert config.effective_rps == 0.0  # Can't calculate without RPM


class TestRPSSmoothingLimiterInit:
    """Limiter initialization tests for RPS smoothing."""

    @pytest.mark.asyncio
    async def test_smoothing_via_constructor_kwargs(self) -> None:
        """RPS smoothing can be set via constructor kwargs."""
        mock_redis = AsyncMock()
        limiter = RateLimiter(mock_redis, "gpt-4", tpm=300_000, rpm=600, smooth_requests=True)
        assert limiter._rps_limit == 10.0  # 600 / 60
        assert limiter._smoothing_interval == 1.0

    @pytest.mark.asyncio
    async def test_smoothing_via_explicit_rps(self) -> None:
        """Explicit RPS can be set via constructor."""
        mock_redis = AsyncMock()
        limiter = RateLimiter(mock_redis, "gpt-4", tpm=300_000, rpm=600, rps=8)
        assert limiter._rps_limit == 8.0

    @pytest.mark.asyncio
    async def test_smoothing_via_config(self) -> None:
        """RPS smoothing can be set via config object."""
        mock_redis = AsyncMock()
        config = RateLimitConfig(tpm=300_000, rpm=600, smooth_requests=True, rps=8)
        limiter = RateLimiter(mock_redis, "gpt-4", config)
        assert limiter._rps_limit == 8.0
        assert limiter._smoothing_interval == 1.0

    @pytest.mark.asyncio
    async def test_custom_smoothing_interval_in_limiter(self) -> None:
        """Custom smoothing interval is applied in limiter."""
        mock_redis = AsyncMock()
        limiter = RateLimiter(
            mock_redis,
            "gpt-4",
            tpm=300_000,
            rpm=600,
            smooth_requests=True,
            smoothing_interval=10.0,
        )
        assert limiter._smoothing_interval == 10.0

    @pytest.mark.asyncio
    async def test_default_smoothing_in_limiter(self) -> None:
        """Default limiter should have RPS smoothing enabled."""
        mock_redis = AsyncMock()
        limiter = RateLimiter(mock_redis, "gpt-4", tpm=100_000, rpm=100)
        assert limiter._rps_limit == 100 / 60.0  # Auto-calculated from RPM

    @pytest.mark.asyncio
    async def test_disabled_smoothing_in_limiter(self) -> None:
        """Limiter with smooth_requests=False should have no RPS smoothing."""
        mock_redis = AsyncMock()
        limiter = RateLimiter(mock_redis, "gpt-4", tpm=100_000, rpm=100, smooth_requests=False)
        assert limiter._rps_limit == 0.0


class TestRPSSmoothingAcquire:
    """Acquire tests for RPS smoothing."""

    @pytest.mark.asyncio
    async def test_rps_limit_passed_to_lua_script(self) -> None:
        """RPS limit should be passed to Lua script."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        limiter = RateLimiter(mock_redis, "gpt-4", tpm=300_000, rpm=600, smooth_requests=True)

        await limiter.acquire(tokens=1000)

        # Check args passed to Lua script
        # ARGV[11] = rps_limit, ARGV[12] = smoothing_interval
        call_args = mock_redis.eval.call_args
        rps_limit = call_args[0][13]  # ARGV[11]
        smoothing_interval = call_args[0][14]  # ARGV[12]
        assert rps_limit == 10.0  # 600 / 60
        assert smoothing_interval == 1.0

    @pytest.mark.asyncio
    async def test_explicit_rps_passed_to_lua_script(self) -> None:
        """Explicit RPS should be passed to Lua script."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        limiter = RateLimiter(mock_redis, "gpt-4", tpm=300_000, rpm=600, rps=8)

        await limiter.acquire(tokens=1000)

        call_args = mock_redis.eval.call_args
        rps_limit = call_args[0][13]
        assert rps_limit == 8.0

    @pytest.mark.asyncio
    async def test_no_rps_passed_when_disabled(self) -> None:
        """RPS limit should be 0 when smoothing is explicitly disabled."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        limiter = RateLimiter(mock_redis, "gpt-4", tpm=100_000, rpm=100, smooth_requests=False)

        await limiter.acquire(tokens=1000)

        call_args = mock_redis.eval.call_args
        rps_limit = call_args[0][13]
        assert rps_limit == 0.0

    @pytest.mark.asyncio
    async def test_custom_interval_passed_to_lua_script(self) -> None:
        """Custom smoothing interval should be passed to Lua script."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        limiter = RateLimiter(
            mock_redis,
            "gpt-4",
            tpm=300_000,
            rpm=600,
            smooth_requests=True,
            smoothing_interval=10.0,
        )

        await limiter.acquire(tokens=1000)

        call_args = mock_redis.eval.call_args
        smoothing_interval = call_args[0][14]
        assert smoothing_interval == 10.0


class TestRPSSmoothingWithOtherFeatures:
    """Tests for RPS smoothing combined with other features."""

    @pytest.mark.asyncio
    async def test_smoothing_with_burndown_rate(self) -> None:
        """RPS smoothing should work together with burndown rate."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        limiter = RateLimiter(
            mock_redis,
            "claude-sonnet",
            tpm=100_000,
            rpm=600,
            burndown_rate=5.0,
            smooth_requests=True,
        )

        await limiter.acquire(input_tokens=3000, output_tokens=1000)

        call_args = mock_redis.eval.call_args
        # Check burndown rate applied to effective_combined
        effective_combined = call_args[0][12]
        assert effective_combined == 8000.0  # 3000 + (5.0 * 1000)
        # Check RPS smoothing enabled
        rps_limit = call_args[0][13]
        assert rps_limit == 10.0

    @pytest.mark.asyncio
    async def test_smoothing_with_burst_multiplier(self) -> None:
        """RPS smoothing should work with burst multiplier."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        limiter = RateLimiter(
            mock_redis,
            "gpt-4",
            tpm=300_000,
            rpm=600,
            burst_multiplier=1.5,
            smooth_requests=True,
        )

        # RPM with burst: 600 * 1.5 = 900
        assert limiter.rpm_limit == 900
        # RPS is calculated from base RPM, not burst-adjusted
        # effective_rps = 600/60 = 10 (from config.effective_rps)
        assert limiter._rps_limit == 10.0

    @pytest.mark.asyncio
    async def test_smoothing_with_split_mode(self) -> None:
        """RPS smoothing should work with split TPM mode."""
        mock_redis = AsyncMock()
        current_time = time.time()
        mock_redis.eval = AsyncMock(return_value=[current_time, 0, "test-id", 0.0])

        limiter = RateLimiter(
            mock_redis,
            "gemini",
            input_tpm=4_000_000,
            output_tpm=128_000,
            rpm=360,
            smooth_requests=True,
        )

        await limiter.acquire(input_tokens=5000, output_tokens=2048)

        call_args = mock_redis.eval.call_args
        # Split mode tokens
        input_tokens_arg = call_args[0][3]
        output_tokens_arg = call_args[0][4]
        assert input_tokens_arg == 5000
        assert output_tokens_arg == 2048
        # RPS smoothing enabled
        rps_limit = call_args[0][13]
        assert rps_limit == 6.0  # 360 / 60
