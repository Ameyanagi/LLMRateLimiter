"""Redis connection management with pooling and retry support."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from redis.asyncio import ConnectionPool, Redis
from redis.exceptions import (
    AuthenticationError,
    BusyLoadingError,
    DataError,
    ResponseError,
)
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
)
from redis.exceptions import (
    TimeoutError as RedisTimeoutError,
)

from llmratelimiter.config import RetryConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Errors that should trigger a retry
RETRYABLE_ERRORS = (
    ConnectionError,
    TimeoutError,
    RedisConnectionError,
    RedisTimeoutError,
    BusyLoadingError,
    OSError,  # Network-related OS errors
)

# Errors that should NOT be retried
NON_RETRYABLE_ERRORS = (
    ResponseError,
    AuthenticationError,
    DataError,
    ValueError,
)


def calculate_delay(attempt: int, config: RetryConfig) -> float:
    """Calculate delay for a retry attempt with exponential backoff and jitter.

    Args:
        attempt: The retry attempt number (0-indexed).
        config: Retry configuration.

    Returns:
        Delay in seconds before the next retry.
    """
    # Exponential backoff: base_delay * (exponential_base ** attempt)
    delay = config.base_delay * (config.exponential_base**attempt)

    # Cap at max_delay
    delay = min(delay, config.max_delay)

    # Add jitter: ±jitter% randomization
    if config.jitter > 0:
        jitter_range = delay * config.jitter
        delay += random.uniform(-jitter_range, jitter_range)

    return max(0, delay)  # Never negative


async def retry_with_backoff(
    operation: Callable[[], Awaitable[T]],
    config: RetryConfig,
    operation_name: str = "operation",
) -> T:
    """Execute an async operation with exponential backoff retry.

    Args:
        operation: Async callable to execute.
        config: Retry configuration.
        operation_name: Name for logging purposes.

    Returns:
        Result of the operation.

    Raises:
        Exception: The last exception if all retries are exhausted.
    """
    last_exception: Exception | None = None

    for attempt in range(config.max_retries + 1):  # +1 for initial attempt
        try:
            return await operation()
        except NON_RETRYABLE_ERRORS:
            # Don't retry these - re-raise immediately
            raise
        except RETRYABLE_ERRORS as e:
            last_exception = e

            if attempt < config.max_retries:
                delay = calculate_delay(attempt, config)
                logger.warning(
                    "%s failed (attempt %d/%d), retrying in %.2fs: %s",
                    operation_name,
                    attempt + 1,
                    config.max_retries + 1,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    "%s failed after %d attempts: %s",
                    operation_name,
                    config.max_retries + 1,
                    e,
                )
        except Exception:
            # Unknown error - log and re-raise
            logger.exception("Unexpected error in %s", operation_name)
            raise

    # All retries exhausted
    if last_exception is not None:
        raise last_exception

    # Should never reach here, but satisfy type checker
    raise RuntimeError("Retry logic error")


class RedisConnectionManager:
    """Manages Redis connections with pooling and retry support.

    Example with URL:
        >>> async with RedisConnectionManager("redis://localhost:6379") as manager:
        ...     client = manager.client
        ...     await client.ping()

    Example with host/port:
        >>> manager = RedisConnectionManager(
        ...     host="localhost",
        ...     port=6379,
        ...     retry_config=RetryConfig(max_retries=5, base_delay=0.2),
        ... )
        >>> limiter = RateLimiter(manager, "gpt-4", tpm=100_000, rpm=100)
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        password: str | None = None,
        max_connections: int = 10,
        retry_config: RetryConfig | None = None,
        decode_responses: bool = True,
        **redis_kwargs: Any,
    ) -> None:
        """Initialize the connection manager.

        Args:
            url: Redis URL (e.g., "redis://localhost:6379/0", "rediss://..." for SSL).
            host: Redis server hostname (used if url is not provided).
            port: Redis server port (used if url is not provided).
            db: Redis database number.
            password: Redis password.
            max_connections: Maximum connections in the pool.
            retry_config: Configuration for retry behavior. Defaults to RetryConfig().
            decode_responses: Whether to decode responses to strings.
            **redis_kwargs: Additional arguments passed to Redis client.
        """
        self._url = url
        self._host = host
        self._port = port
        self._db = db
        self._password = password
        self._max_connections = max_connections
        self._retry_config = retry_config or RetryConfig()
        self._decode_responses = decode_responses
        self._redis_kwargs = redis_kwargs

        self._pool: ConnectionPool | None = None
        self._client: Redis | None = None

    @property
    def retry_config(self) -> RetryConfig:
        """Get the retry configuration."""
        return self._retry_config

    @property
    def client(self) -> Redis:
        """Get the Redis client, creating the pool if needed."""
        if self._client is None:
            # Build common kwargs
            pool_kwargs: dict[str, Any] = {
                "max_connections": self._max_connections,
                "decode_responses": self._decode_responses,
                **self._redis_kwargs,
            }

            if self._url is not None:
                # Use URL-based connection pool (use rediss:// for SSL)
                # Override db/password if explicitly provided
                if self._db != 0:
                    pool_kwargs["db"] = self._db
                if self._password is not None:
                    pool_kwargs["password"] = self._password

                self._pool = ConnectionPool.from_url(self._url, **pool_kwargs)
            else:
                # Use host/port-based connection pool
                self._pool = ConnectionPool(
                    host=self._host,
                    port=self._port,
                    db=self._db,
                    password=self._password,
                    **pool_kwargs,
                )
            self._client = Redis(connection_pool=self._pool)
        return self._client

    async def close(self) -> None:
        """Close all connections in the pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._pool is not None:
            await self._pool.disconnect()
            self._pool = None

    async def __aenter__(self) -> "RedisConnectionManager":
        """Enter async context manager."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Exit async context manager, closing connections."""
        await self.close()
