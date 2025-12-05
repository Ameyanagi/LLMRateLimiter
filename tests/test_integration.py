"""Integration tests with real Redis.

These tests require a running Redis instance. They are skipped if Redis
is not available.
"""

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from llmratelimiter import RateLimitConfig, RateLimiter
from llmratelimiter.config import RetryConfig
from llmratelimiter.connection import RedisConnectionManager


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """Create Redis client for testing.

    Uses REDIS_HOST and REDIS_PORT env vars or defaults to localhost:6379.
    Uses database 15 to avoid conflicts with production data.
    """
    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))

    client = Redis(host=redis_host, port=redis_port, db=15, decode_responses=True)

    try:
        await client.ping()
    except Exception as e:
        await client.aclose()
        pytest.skip(f"Redis not available: {e}")

    # Clean up any existing test keys
    await client.flushdb()

    yield client

    # Cleanup
    await client.flushdb()
    await client.aclose()


class TestCombinedModeIntegration:
    """Integration tests for combined mode (tpm > 0)."""

    @pytest.mark.asyncio
    async def test_immediate_acquire_under_limits(self, redis_client: Redis) -> None:
        """Requests under limits should be immediate."""
        config = RateLimitConfig(tpm=100_000, rpm=100)
        limiter = RateLimiter(redis_client, "test-model", config)

        result = await limiter.acquire(tokens=5000)

        assert result.wait_time == 0.0
        assert result.queue_position == 0

    @pytest.mark.asyncio
    async def test_multiple_requests_increment_usage(self, redis_client: Redis) -> None:
        """Multiple requests should accumulate usage."""
        config = RateLimitConfig(tpm=100_000, rpm=100)
        limiter = RateLimiter(redis_client, "test-model-usage", config)

        await limiter.acquire(tokens=10000)
        await limiter.acquire(tokens=20000)
        await limiter.acquire(tokens=15000)

        status = await limiter.get_status()

        assert status.tokens_used == 45000
        assert status.requests_used == 3

    @pytest.mark.asyncio
    async def test_rpm_limit_causes_wait(self, redis_client: Redis) -> None:
        """Exceeding RPM limit should cause wait time."""
        config = RateLimitConfig(tpm=1_000_000, rpm=2, window_seconds=1)
        limiter = RateLimiter(redis_client, "test-model-rpm", config)

        # First two should be immediate
        result1 = await limiter.acquire(tokens=100)
        result2 = await limiter.acquire(tokens=100)

        assert result1.wait_time == 0.0
        assert result2.wait_time == 0.0

        # Third should be queued
        limiter._should_wait = False
        result3 = await limiter.acquire(tokens=100)

        assert result3.queue_position >= 1
        assert result3.wait_time > 0

    @pytest.mark.asyncio
    async def test_tpm_limit_causes_wait(self, redis_client: Redis) -> None:
        """Exceeding TPM limit should cause wait time."""
        config = RateLimitConfig(tpm=10_000, rpm=100, window_seconds=1)
        limiter = RateLimiter(redis_client, "test-model-tpm", config)

        # Consume most of the limit
        await limiter.acquire(tokens=9000)

        # This should exceed the limit
        limiter._should_wait = False
        result = await limiter.acquire(tokens=5000)

        assert result.wait_time > 0

    @pytest.mark.asyncio
    async def test_capacity_freed_after_window(self, redis_client: Redis) -> None:
        """Capacity should be freed after window expires."""
        config = RateLimitConfig(tpm=10_000, rpm=100, window_seconds=1)
        limiter = RateLimiter(redis_client, "test-model-expiry", config)

        # Consume capacity
        await limiter.acquire(tokens=9000)

        # Wait for window to expire
        await asyncio.sleep(1.1)

        # Should be immediate now
        result = await limiter.acquire(tokens=9000)

        assert result.wait_time == 0.0

    @pytest.mark.asyncio
    async def test_fifo_ordering(self, redis_client: Redis) -> None:
        """Requests should be processed in FIFO order."""
        config = RateLimitConfig(tpm=100_000, rpm=2, window_seconds=60)
        limiter = RateLimiter(redis_client, "test-model-fifo", config)

        # Fill capacity
        await limiter.acquire(tokens=100)
        await limiter.acquire(tokens=100)

        # Queue multiple requests
        limiter._should_wait = False
        results = []
        for _ in range(5):
            result = await limiter.acquire(tokens=100)
            results.append(result)

        # Queue positions should increment
        positions = [r.queue_position for r in results]
        assert positions == sorted(positions)

        # Slot times should be monotonically increasing
        slot_times = [r.slot_time for r in results]
        for i in range(1, len(slot_times)):
            assert slot_times[i] >= slot_times[i - 1]


class TestSplitModeIntegration:
    """Integration tests for split mode (input_tpm/output_tpm > 0)."""

    @pytest.mark.asyncio
    async def test_immediate_acquire_under_all_limits(self, redis_client: Redis) -> None:
        """Requests under all limits should be immediate."""
        config = RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)
        limiter = RateLimiter(redis_client, "gemini-test", config)

        result = await limiter.acquire(input_tokens=10000, output_tokens=5000)

        assert result.wait_time == 0.0
        assert result.queue_position == 0

    @pytest.mark.asyncio
    async def test_output_tpm_limit_causes_wait(self, redis_client: Redis) -> None:
        """Exceeding output TPM limit should cause wait."""
        config = RateLimitConfig(input_tpm=1_000_000, output_tpm=10_000, rpm=100, window_seconds=1)
        limiter = RateLimiter(redis_client, "gemini-output", config)

        # Consume most of output limit
        await limiter.acquire(input_tokens=100, output_tokens=9000)

        # This should exceed output limit
        limiter._should_wait = False
        result = await limiter.acquire(input_tokens=100, output_tokens=5000)

        assert result.wait_time > 0

    @pytest.mark.asyncio
    async def test_adjust_updates_record(self, redis_client: Redis) -> None:
        """adjust() should update the output tokens."""
        config = RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)
        limiter = RateLimiter(redis_client, "gemini-adjust", config)

        # Acquire with estimate
        result = await limiter.acquire(input_tokens=5000, output_tokens=10000)

        status_before = await limiter.get_status()
        assert status_before.output_tokens_used == 10000

        # Adjust to actual (lower)
        await limiter.adjust(result.record_id, actual_output=2000)

        status_after = await limiter.get_status()
        assert status_after.output_tokens_used == 2000

    @pytest.mark.asyncio
    async def test_status_shows_split_info(self, redis_client: Redis) -> None:
        """get_status() should show input and output separately."""
        config = RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)
        limiter = RateLimiter(redis_client, "gemini-status", config)

        await limiter.acquire(input_tokens=10000, output_tokens=5000)
        await limiter.acquire(input_tokens=20000, output_tokens=3000)

        status = await limiter.get_status()

        assert status.input_tokens_used == 30000
        assert status.output_tokens_used == 8000
        assert status.requests_used == 2


class TestMixedModeIntegration:
    """Integration tests for mixed mode (tpm + input_tpm + output_tpm)."""

    @pytest.mark.asyncio
    async def test_mixed_mode_all_limits_enforced(self, redis_client: Redis) -> None:
        """All three limits should be enforced in mixed mode."""
        # Combined limit of 10K, input limit of 8K, output limit of 3K
        config = RateLimitConfig(tpm=10_000, input_tpm=8_000, output_tpm=3_000, rpm=100, window_seconds=1)
        limiter = RateLimiter(redis_client, "mixed-test", config)

        # This uses 5K input + 2K output = 7K combined (under all limits)
        result = await limiter.acquire(input_tokens=5000, output_tokens=2000)
        assert result.wait_time == 0.0

    @pytest.mark.asyncio
    async def test_mixed_mode_combined_limit_triggers_wait(self, redis_client: Redis) -> None:
        """Combined limit should trigger wait even if split limits are OK."""
        # Combined limit of 5K, but input/output limits are high
        config = RateLimitConfig(tpm=5_000, input_tpm=100_000, output_tpm=100_000, rpm=100, window_seconds=1)
        limiter = RateLimiter(redis_client, "mixed-combined", config)

        # Use 4K combined
        await limiter.acquire(input_tokens=2000, output_tokens=2000)

        # This would add 3K more = 7K total, exceeding combined limit
        limiter._should_wait = False
        result = await limiter.acquire(input_tokens=1500, output_tokens=1500)

        assert result.wait_time > 0

    @pytest.mark.asyncio
    async def test_mixed_mode_input_limit_triggers_wait(self, redis_client: Redis) -> None:
        """Input limit should trigger wait even if combined limit is OK."""
        # High combined limit, but low input limit
        config = RateLimitConfig(tpm=100_000, input_tpm=5_000, output_tpm=100_000, rpm=100, window_seconds=1)
        limiter = RateLimiter(redis_client, "mixed-input", config)

        # Use 4K input
        await limiter.acquire(input_tokens=4000, output_tokens=100)

        # This would add 3K input = 7K total input, exceeding input limit
        limiter._should_wait = False
        result = await limiter.acquire(input_tokens=3000, output_tokens=100)

        assert result.wait_time > 0

    @pytest.mark.asyncio
    async def test_mixed_mode_output_limit_triggers_wait(self, redis_client: Redis) -> None:
        """Output limit should trigger wait even if other limits are OK."""
        # High combined and input limits, but low output limit
        config = RateLimitConfig(tpm=100_000, input_tpm=100_000, output_tpm=5_000, rpm=100, window_seconds=1)
        limiter = RateLimiter(redis_client, "mixed-output", config)

        # Use 4K output
        await limiter.acquire(input_tokens=100, output_tokens=4000)

        # This would add 3K output = 7K total output, exceeding output limit
        limiter._should_wait = False
        result = await limiter.acquire(input_tokens=100, output_tokens=3000)

        assert result.wait_time > 0

    @pytest.mark.asyncio
    async def test_mixed_mode_status_shows_all_info(self, redis_client: Redis) -> None:
        """Status should show combined and split info in mixed mode."""
        config = RateLimitConfig(tpm=100_000, input_tpm=80_000, output_tpm=20_000, rpm=100)
        limiter = RateLimiter(redis_client, "mixed-status", config)

        await limiter.acquire(input_tokens=5000, output_tokens=2000)

        status = await limiter.get_status()

        assert status.tokens_used == 7000  # combined
        assert status.tokens_limit == 100_000
        assert status.input_tokens_used == 5000
        assert status.input_tokens_limit == 80_000
        assert status.output_tokens_used == 2000
        assert status.output_tokens_limit == 20_000


class TestConfigValidation:
    """Tests for configuration validation."""

    def test_mixed_mode_config_is_valid(self) -> None:
        """Can specify both tpm and input_tpm/output_tpm."""
        config = RateLimitConfig(tpm=100_000, input_tpm=80_000, output_tpm=20_000, rpm=100)
        assert config.is_split_mode
        assert config.has_combined_limit

    def test_combined_mode_config(self) -> None:
        """Combined mode config is valid."""
        config = RateLimitConfig(tpm=100_000, rpm=100)
        assert not config.is_split_mode
        assert config.has_combined_limit

    def test_split_mode_config(self) -> None:
        """Split mode config is valid."""
        config = RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)
        assert config.is_split_mode
        assert not config.has_combined_limit

    def test_rpm_only_config(self) -> None:
        """RPM-only config is valid (no TPM limits)."""
        config = RateLimitConfig(rpm=100)
        assert not config.is_split_mode
        assert not config.has_combined_limit


class TestDisabledLimits:
    """Tests for disabled limits (set to 0)."""

    @pytest.mark.asyncio
    async def test_disabled_rpm_allows_unlimited_requests(self, redis_client: Redis) -> None:
        """rpm=0 should allow unlimited requests."""
        config = RateLimitConfig(tpm=100_000, rpm=0)
        limiter = RateLimiter(redis_client, "test-no-rpm", config)

        # Should all be immediate even with many requests
        for _ in range(10):
            result = await limiter.acquire(tokens=100)
            assert result.wait_time == 0.0


class TestConcurrentRequests:
    """Tests for concurrent request handling."""

    @pytest.mark.asyncio
    async def test_concurrent_requests_get_unique_positions(self, redis_client: Redis) -> None:
        """Concurrent requests should get unique queue positions."""
        config = RateLimitConfig(tpm=100_000, rpm=2, window_seconds=60)
        limiter = RateLimiter(redis_client, "test-concurrent", config)

        # Fill immediate capacity
        await limiter.acquire(tokens=100)
        await limiter.acquire(tokens=100)

        # Submit concurrent requests
        limiter._should_wait = False
        tasks = [limiter.acquire(tokens=100) for _ in range(5)]
        results = await asyncio.gather(*tasks)

        # All should have unique queue positions
        positions = [r.queue_position for r in results]
        assert len(set(positions)) == 5

        # All record IDs should be unique
        record_ids = [r.record_id for r in results]
        assert len(set(record_ids)) == 5


class TestLongWaitTimes:
    """Tests for long wait time calculations."""

    @pytest.mark.asyncio
    async def test_wait_time_calculation_for_deep_queue(self, redis_client: Redis) -> None:
        """Deep queue should calculate correct wait times."""
        config = RateLimitConfig(tpm=5_000, rpm=100, window_seconds=60)
        limiter = RateLimiter(redis_client, "test-deep-queue", config)

        # Fill capacity
        await limiter.acquire(tokens=5000)

        # Queue multiple requests that each need the full capacity
        limiter._should_wait = False
        results = []
        for _ in range(5):
            result = await limiter.acquire(tokens=5000)
            results.append(result)

        # Each subsequent request should wait longer
        wait_times = [r.wait_time for r in results]
        for i in range(1, len(wait_times)):
            assert wait_times[i] >= wait_times[i - 1]

        # Last request should wait > 4 minutes (5 * 60s - some overlap)
        assert wait_times[-1] >= 200


class TestConnectionManagerIntegration:
    """Integration tests for RedisConnectionManager."""

    @pytest.fixture
    async def connection_manager(self) -> AsyncIterator[RedisConnectionManager]:
        """Create connection manager for testing."""
        redis_host = os.environ.get("REDIS_HOST", "localhost")
        redis_port = int(os.environ.get("REDIS_PORT", "6379"))

        manager = RedisConnectionManager(
            host=redis_host,
            port=redis_port,
            db=15,
            retry_config=RetryConfig(max_retries=2, base_delay=0.05),
        )

        try:
            await manager.client.ping()
        except Exception as e:
            await manager.close()
            pytest.skip(f"Redis not available: {e}")

        await manager.client.flushdb()

        yield manager

        await manager.client.flushdb()
        await manager.close()

    @pytest.mark.asyncio
    async def test_limiter_with_connection_manager(self, connection_manager: RedisConnectionManager) -> None:
        """RateLimiter should work with RedisConnectionManager."""
        config = RateLimitConfig(tpm=100_000, rpm=100)
        limiter = RateLimiter(connection_manager, "test-manager", config)

        result = await limiter.acquire(tokens=5000)

        assert result.wait_time == 0.0
        assert result.queue_position == 0

    @pytest.mark.asyncio
    async def test_connection_manager_ping(self, connection_manager: RedisConnectionManager) -> None:
        """Connection manager client should be able to ping Redis."""
        result = await connection_manager.client.ping()
        assert result is True

    @pytest.mark.asyncio
    async def test_multiple_limiters_share_manager(self, connection_manager: RedisConnectionManager) -> None:
        """Multiple limiters can share a connection manager."""
        config1 = RateLimitConfig(tpm=100_000, rpm=100)
        config2 = RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)

        limiter1 = RateLimiter(connection_manager, "model-1", config1)
        limiter2 = RateLimiter(connection_manager, "model-2", config2)

        result1 = await limiter1.acquire(tokens=5000)
        result2 = await limiter2.acquire(input_tokens=10000, output_tokens=5000)

        assert result1.wait_time == 0.0
        assert result2.wait_time == 0.0

        # Both should use the same Redis client
        assert limiter1.redis is limiter2.redis

    @pytest.mark.asyncio
    async def test_context_manager_usage(self) -> None:
        """Connection manager should work as async context manager."""
        redis_host = os.environ.get("REDIS_HOST", "localhost")
        redis_port = int(os.environ.get("REDIS_PORT", "6379"))

        async with RedisConnectionManager(host=redis_host, port=redis_port, db=15) as manager:
            try:
                await manager.client.ping()
            except Exception as e:
                pytest.skip(f"Redis not available: {e}")

            config = RateLimitConfig(tpm=100_000, rpm=100)
            limiter = RateLimiter(manager, "context-test", config)

            result = await limiter.acquire(tokens=1000)
            assert result.wait_time == 0.0
