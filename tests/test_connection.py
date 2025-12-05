"""Tests for Redis connection management and retry logic."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from redis.exceptions import (
    AuthenticationError,
    BusyLoadingError,
    ResponseError,
)
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
)
from redis.exceptions import (
    TimeoutError as RedisTimeoutError,
)

from llmratelimiter.config import RetryConfig
from llmratelimiter.connection import (
    RedisConnectionManager,
    calculate_delay,
    retry_with_backoff,
)


class TestRetryConfig:
    """Tests for RetryConfig dataclass."""

    def test_default_values(self) -> None:
        """Default values should be sensible."""
        config = RetryConfig()
        assert config.max_retries == 3
        assert config.base_delay == 0.1
        assert config.max_delay == 5.0
        assert config.exponential_base == 2.0
        assert config.jitter == 0.1

    def test_custom_values(self) -> None:
        """Custom values should be accepted."""
        config = RetryConfig(
            max_retries=5,
            base_delay=0.2,
            max_delay=10.0,
            exponential_base=3.0,
            jitter=0.2,
        )
        assert config.max_retries == 5
        assert config.base_delay == 0.2
        assert config.max_delay == 10.0
        assert config.exponential_base == 3.0
        assert config.jitter == 0.2

    def test_zero_retries_allowed(self) -> None:
        """Zero retries should be allowed (no retry)."""
        config = RetryConfig(max_retries=0)
        assert config.max_retries == 0

    def test_negative_retries_rejected(self) -> None:
        """Negative max_retries should raise ValueError."""
        with pytest.raises(ValueError, match="max_retries must be >= 0"):
            RetryConfig(max_retries=-1)

    def test_zero_base_delay_rejected(self) -> None:
        """Zero base_delay should raise ValueError."""
        with pytest.raises(ValueError, match="base_delay must be > 0"):
            RetryConfig(base_delay=0)

    def test_negative_base_delay_rejected(self) -> None:
        """Negative base_delay should raise ValueError."""
        with pytest.raises(ValueError, match="base_delay must be > 0"):
            RetryConfig(base_delay=-0.1)

    def test_max_delay_less_than_base_rejected(self) -> None:
        """max_delay < base_delay should raise ValueError."""
        with pytest.raises(ValueError, match="max_delay must be >= base_delay"):
            RetryConfig(base_delay=1.0, max_delay=0.5)

    def test_exponential_base_less_than_one_rejected(self) -> None:
        """exponential_base < 1 should raise ValueError."""
        with pytest.raises(ValueError, match="exponential_base must be >= 1"):
            RetryConfig(exponential_base=0.5)

    def test_jitter_negative_rejected(self) -> None:
        """Negative jitter should raise ValueError."""
        with pytest.raises(ValueError, match="jitter must be between 0 and 1"):
            RetryConfig(jitter=-0.1)

    def test_jitter_greater_than_one_rejected(self) -> None:
        """Jitter > 1 should raise ValueError."""
        with pytest.raises(ValueError, match="jitter must be between 0 and 1"):
            RetryConfig(jitter=1.5)

    def test_config_is_frozen(self) -> None:
        """Config should be immutable."""
        config = RetryConfig()
        with pytest.raises(AttributeError):
            config.max_retries = 10  # type: ignore[misc]


class TestCalculateDelay:
    """Tests for calculate_delay function."""

    def test_first_attempt_uses_base_delay(self) -> None:
        """First attempt (0) should use base_delay."""
        config = RetryConfig(base_delay=0.1, jitter=0)
        delay = calculate_delay(0, config)
        assert delay == pytest.approx(0.1, abs=0.001)

    def test_exponential_growth(self) -> None:
        """Delay should grow exponentially."""
        config = RetryConfig(base_delay=0.1, exponential_base=2.0, jitter=0)

        assert calculate_delay(0, config) == pytest.approx(0.1, abs=0.001)
        assert calculate_delay(1, config) == pytest.approx(0.2, abs=0.001)
        assert calculate_delay(2, config) == pytest.approx(0.4, abs=0.001)
        assert calculate_delay(3, config) == pytest.approx(0.8, abs=0.001)

    def test_max_delay_cap(self) -> None:
        """Delay should be capped at max_delay."""
        config = RetryConfig(base_delay=0.1, max_delay=1.0, exponential_base=2.0, jitter=0)

        # At attempt 10, exponential would be 0.1 * 2^10 = 102.4, but capped at 1.0
        delay = calculate_delay(10, config)
        assert delay == pytest.approx(1.0, abs=0.001)

    def test_jitter_adds_randomness(self) -> None:
        """Jitter should add randomness to delay."""
        config = RetryConfig(base_delay=1.0, jitter=0.1)

        delays = [calculate_delay(0, config) for _ in range(100)]

        # All delays should be within ±10% of base
        for delay in delays:
            assert 0.9 <= delay <= 1.1

        # Should have some variation (not all the same)
        assert len(set(delays)) > 1

    def test_delay_never_negative(self) -> None:
        """Delay should never be negative even with jitter."""
        config = RetryConfig(base_delay=0.01, jitter=1.0)  # Max jitter

        for _ in range(100):
            delay = calculate_delay(0, config)
            assert delay >= 0


class TestRetryWithBackoff:
    """Tests for retry_with_backoff function."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self) -> None:
        """Should return immediately on success."""
        operation = AsyncMock(return_value="success")
        config = RetryConfig(max_retries=3)

        result = await retry_with_backoff(operation, config, "test")

        assert result == "success"
        assert operation.call_count == 1

    @pytest.mark.asyncio
    async def test_success_after_retry(self) -> None:
        """Should succeed after transient failure."""
        operation = AsyncMock(side_effect=[RedisConnectionError("fail"), "success"])
        config = RetryConfig(max_retries=3, base_delay=0.01)

        result = await retry_with_backoff(operation, config, "test")

        assert result == "success"
        assert operation.call_count == 2

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self) -> None:
        """Should raise after max retries exhausted."""
        operation = AsyncMock(side_effect=RedisConnectionError("always fails"))
        config = RetryConfig(max_retries=2, base_delay=0.01)

        with pytest.raises(RedisConnectionError, match="always fails"):
            await retry_with_backoff(operation, config, "test")

        # Initial attempt + 2 retries = 3 calls
        assert operation.call_count == 3

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self) -> None:
        """Non-retryable errors should not be retried."""
        operation = AsyncMock(side_effect=ResponseError("script error"))
        config = RetryConfig(max_retries=3, base_delay=0.01)

        with pytest.raises(ResponseError, match="script error"):
            await retry_with_backoff(operation, config, "test")

        # Should only be called once - no retry
        assert operation.call_count == 1

    @pytest.mark.asyncio
    async def test_authentication_error_not_retried(self) -> None:
        """AuthenticationError should not be retried."""
        operation = AsyncMock(side_effect=AuthenticationError("bad password"))
        config = RetryConfig(max_retries=3, base_delay=0.01)

        with pytest.raises(AuthenticationError):
            await retry_with_backoff(operation, config, "test")

        assert operation.call_count == 1

    @pytest.mark.asyncio
    async def test_timeout_error_is_retried(self) -> None:
        """TimeoutError should be retried."""
        operation = AsyncMock(side_effect=[RedisTimeoutError("timeout"), "success"])
        config = RetryConfig(max_retries=3, base_delay=0.01)

        result = await retry_with_backoff(operation, config, "test")

        assert result == "success"
        assert operation.call_count == 2

    @pytest.mark.asyncio
    async def test_busy_loading_error_is_retried(self) -> None:
        """BusyLoadingError should be retried."""
        operation = AsyncMock(side_effect=[BusyLoadingError("loading"), "success"])
        config = RetryConfig(max_retries=3, base_delay=0.01)

        result = await retry_with_backoff(operation, config, "test")

        assert result == "success"
        assert operation.call_count == 2

    @pytest.mark.asyncio
    async def test_zero_retries_no_retry(self) -> None:
        """With max_retries=0, should not retry."""
        operation = AsyncMock(side_effect=RedisConnectionError("fail"))
        config = RetryConfig(max_retries=0, base_delay=0.01)

        with pytest.raises(RedisConnectionError):
            await retry_with_backoff(operation, config, "test")

        # Only initial attempt
        assert operation.call_count == 1

    @pytest.mark.asyncio
    async def test_exponential_delay_timing(self) -> None:
        """Should wait with exponential backoff between retries."""
        operation = AsyncMock(
            side_effect=[
                RedisConnectionError("1"),
                RedisConnectionError("2"),
                "success",
            ]
        )
        config = RetryConfig(max_retries=3, base_delay=0.05, jitter=0)

        sleep_times: list[float] = []
        original_sleep = asyncio.sleep

        async def mock_sleep(delay: float) -> None:
            sleep_times.append(delay)
            await original_sleep(0.001)  # Minimal actual sleep

        with patch("llmratelimiter.connection.asyncio.sleep", mock_sleep):
            await retry_with_backoff(operation, config, "test")

        # Should have slept twice (after first and second failure)
        assert len(sleep_times) == 2
        # First delay: 0.05, second: 0.10
        assert sleep_times[0] == pytest.approx(0.05, abs=0.01)
        assert sleep_times[1] == pytest.approx(0.10, abs=0.01)


class TestRedisConnectionManager:
    """Tests for RedisConnectionManager class."""

    def test_default_values(self) -> None:
        """Default values should be sensible."""
        manager = RedisConnectionManager()
        assert manager._host == "localhost"
        assert manager._port == 6379
        assert manager._db == 0
        assert manager._max_connections == 10
        assert isinstance(manager.retry_config, RetryConfig)

    def test_custom_values(self) -> None:
        """Custom values should be accepted."""
        retry_config = RetryConfig(max_retries=5)
        manager = RedisConnectionManager(
            host="redis.example.com",
            port=6380,
            db=1,
            password="secret",
            max_connections=20,
            retry_config=retry_config,
        )
        assert manager._host == "redis.example.com"
        assert manager._port == 6380
        assert manager._db == 1
        assert manager._password == "secret"
        assert manager._max_connections == 20
        assert manager.retry_config.max_retries == 5

    def test_client_property_creates_pool(self) -> None:
        """Accessing client should create the connection pool."""
        manager = RedisConnectionManager()

        assert manager._pool is None
        assert manager._client is None

        client = manager.client

        assert manager._pool is not None
        assert manager._client is not None
        assert client is manager._client

    def test_client_property_reuses_pool(self) -> None:
        """Accessing client multiple times should reuse the pool."""
        manager = RedisConnectionManager()

        client1 = manager.client
        client2 = manager.client

        assert client1 is client2

    @pytest.mark.asyncio
    async def test_close_cleans_up(self) -> None:
        """close() should clean up client and pool."""
        manager = RedisConnectionManager()
        _ = manager.client  # Create the pool

        assert manager._client is not None
        assert manager._pool is not None

        await manager.close()

        assert manager._client is None
        assert manager._pool is None

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        """Context manager should close on exit."""
        async with RedisConnectionManager() as manager:
            _ = manager.client
            assert manager._client is not None

        assert manager._client is None

    @pytest.mark.asyncio
    async def test_close_idempotent(self) -> None:
        """close() should be safe to call multiple times."""
        manager = RedisConnectionManager()
        _ = manager.client

        await manager.close()
        await manager.close()  # Should not raise

        assert manager._client is None
