"""Unified rate limiter implementation."""

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, overload

from redis.asyncio import Redis

from llmratelimiter.config import RateLimitConfig, RetryConfig
from llmratelimiter.connection import (
    RETRYABLE_ERRORS,
    RedisConnectionManager,
    retry_with_backoff,
)
from llmratelimiter.models import AcquireResult, RateLimitStatus
from llmratelimiter.scripts import ACQUIRE_SCRIPT, ADJUST_SCRIPT, STATUS_SCRIPT

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Type alias for redis parameter
RedisClient = str | Redis | RedisConnectionManager


class RateLimiter:
    """Unified rate limiter for LLM API calls.

    Supports combined TPM, split TPM, or both based on the configuration.

    Simple URL example:
        >>> limiter = RateLimiter("redis://localhost:6379", "gpt-4", tpm=100_000, rpm=100)
        >>> await limiter.acquire(tokens=5000)

    Split mode example (GCP Vertex AI):
        >>> limiter = RateLimiter("redis://localhost", "gemini-1.5-pro",
        ...                       input_tpm=4_000_000, output_tpm=128_000, rpm=360)
        >>> result = await limiter.acquire(input_tokens=5000, output_tokens=2048)
        >>> await limiter.adjust(result.record_id, actual_output=1500)

    With existing Redis client:
        >>> limiter = RateLimiter(redis=existing_client, model="gpt-4", tpm=100_000, rpm=100)

    With connection manager (includes retry support):
        >>> manager = RedisConnectionManager("redis://localhost", retry_config=RetryConfig())
        >>> limiter = RateLimiter(manager, "gpt-4", tpm=100_000, rpm=100)

    With config object (advanced):
        >>> config = RateLimitConfig(tpm=100_000, rpm=100, burst_multiplier=1.5)
        >>> limiter = RateLimiter("redis://localhost", "gpt-4", config=config)
    """

    def __init__(
        self,
        redis: RedisClient | None = None,
        model: str | None = None,
        config: RateLimitConfig | None = None,
        *,
        # Rate limit kwargs (alternative to config)
        tpm: int = 0,
        rpm: int = 0,
        input_tpm: int = 0,
        output_tpm: int = 0,
        window_seconds: int = 60,
        burst_multiplier: float = 1.0,
        # Redis connection kwargs (for URL connections)
        password: str | None = None,
        db: int = 0,
        max_connections: int = 10,
        ssl: bool = False,
        retry_config: RetryConfig | None = None,
        # Legacy positional support
        redis_client: Redis | RedisConnectionManager | None = None,
        model_name: str | None = None,
    ) -> None:
        """Initialize the rate limiter.

        Args:
            redis: Redis URL string, async Redis client, or RedisConnectionManager.
            model: Name of the model (used for Redis key namespace).
            config: Configuration for rate limits (optional if using kwargs).
            tpm: Combined tokens per minute limit.
            rpm: Requests per minute limit.
            input_tpm: Input tokens per minute limit (split mode).
            output_tpm: Output tokens per minute limit (split mode).
            window_seconds: Sliding window duration in seconds.
            burst_multiplier: Multiplier for burst capacity.
            password: Redis password (for URL connections).
            db: Redis database number (for URL connections).
            max_connections: Maximum connections in pool (for URL connections).
            ssl: Enable SSL/TLS for Redis connection.
            retry_config: Retry configuration for URL-based connections.
            redis_client: Deprecated, use 'redis' parameter.
            model_name: Deprecated, use 'model' parameter.
        """
        # Handle legacy parameter names for backward compatibility
        if redis_client is not None and redis is None:
            redis = redis_client
        if model_name is not None and model is None:
            model = model_name

        if redis is None:
            raise ValueError("redis parameter is required (URL string, Redis client, or RedisConnectionManager)")
        if model is None:
            raise ValueError("model parameter is required")

        # Handle different redis parameter types
        if isinstance(redis, str):
            # URL string - create a connection manager
            self._manager: RedisConnectionManager | None = RedisConnectionManager(
                url=redis,
                password=password,
                db=db,
                max_connections=max_connections,
                ssl=ssl,
                retry_config=retry_config,
            )
            self.redis = self._manager.client
            self._retry_config: RetryConfig | None = self._manager.retry_config
        elif isinstance(redis, RedisConnectionManager):
            self._manager = redis
            self.redis = redis.client
            self._retry_config = redis.retry_config
        else:
            # Raw Redis client
            self._manager = None
            self.redis = redis
            self._retry_config = retry_config

        self.model_name = model

        # Build config from kwargs if not provided
        if config is None:
            config = RateLimitConfig(
                tpm=tpm,
                rpm=rpm,
                input_tpm=input_tpm,
                output_tpm=output_tpm,
                window_seconds=window_seconds,
                burst_multiplier=burst_multiplier,
            )

        self.window_seconds = config.window_seconds
        self.burst_multiplier = config.burst_multiplier
        self._config = config

        # Calculate effective limits with burst multiplier
        self.rpm_limit = int(config.rpm * config.burst_multiplier) if config.rpm > 0 else 0
        self.tpm_limit = int(config.tpm * config.burst_multiplier) if config.tpm > 0 else 0
        self.input_tpm_limit = int(config.input_tpm * config.burst_multiplier) if config.input_tpm > 0 else 0
        self.output_tpm_limit = int(config.output_tpm * config.burst_multiplier) if config.output_tpm > 0 else 0

        # Redis key for consumption records
        self.consumption_key = f"rate_limit:{model}:consumption"

        # Lua scripts
        self._acquire_script = ACQUIRE_SCRIPT
        self._adjust_script = ADJUST_SCRIPT
        self._status_script = STATUS_SCRIPT

        # For testing - can be set to False to skip actual waiting
        self._should_wait = True

    @property
    def is_split_mode(self) -> bool:
        """Whether this limiter uses split input/output TPM limits."""
        return self._config.is_split_mode

    @property
    def has_combined_limit(self) -> bool:
        """Whether this limiter has a combined TPM limit."""
        return self._config.has_combined_limit

    @overload
    async def acquire(self, *, tokens: int) -> AcquireResult:
        """Acquire for combined mode - tokens counted as input."""
        ...

    @overload
    async def acquire(self, *, input_tokens: int, output_tokens: int = 0) -> AcquireResult:
        """Acquire for split/mixed mode."""
        ...

    async def acquire(
        self,
        *,
        tokens: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int = 0,
    ) -> AcquireResult:
        """Acquire rate limit capacity.

        For combined mode only, use tokens parameter:
            await limiter.acquire(tokens=5000)

        For split or mixed mode, use input_tokens/output_tokens:
            await limiter.acquire(input_tokens=5000, output_tokens=2048)

        Blocks until capacity is available (FIFO ordering), then returns.
        On Redis failure (after retries if configured), allows the request
        (graceful degradation).

        Args:
            tokens: Number of tokens (treated as input_tokens, output_tokens=0).
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens (default 0).

        Returns:
            AcquireResult with slot time, wait time, queue position, and record ID.
        """
        # Resolve input tokens
        if tokens is not None:
            if input_tokens is not None:
                raise ValueError("Cannot specify both tokens and input_tokens")
            input_tokens = tokens

        if input_tokens is None:
            raise ValueError("Must specify either tokens or input_tokens")

        return await self._execute_acquire(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def adjust(self, record_id: str, actual_output: int) -> None:
        """Adjust the output tokens for a consumption record.

        Use this when the actual output tokens differ from the estimate.
        This frees up capacity if actual < estimated, or uses more if actual > estimated.

        Args:
            record_id: The record ID from the acquire() result.
            actual_output: The actual number of output tokens.
        """

        async def do_adjust() -> None:
            result = await self.redis.eval(  # type: ignore[misc]
                self._adjust_script,
                1,
                self.consumption_key,
                record_id,
                actual_output,
            )
            if result[0] == 0:
                logger.warning("Record not found for adjustment: %s", record_id)

        try:
            if self._retry_config is not None:
                await retry_with_backoff(do_adjust, self._retry_config, "adjust")
            else:
                await do_adjust()
        except RETRYABLE_ERRORS as e:
            logger.warning("Failed to adjust record %s: %s", record_id, e)
        except Exception as e:
            logger.warning("Failed to adjust record %s: %s", record_id, e)

    async def get_status(self) -> RateLimitStatus:
        """Get current rate limit status.

        Returns:
            RateLimitStatus with current usage and limits.
        """
        current_time = time.time()

        async def do_get_status() -> tuple[int, int, int, int]:
            result = await self.redis.eval(  # type: ignore[misc]
                self._status_script,
                1,
                self.consumption_key,
                current_time,
                self.window_seconds,
            )
            return (
                int(result[0]),
                int(result[1]),
                int(result[2]),
                int(result[3]),
            )

        try:
            if self._retry_config is not None:
                total_input, total_output, total_requests, queue_depth = await retry_with_backoff(
                    do_get_status, self._retry_config, "get_status"
                )
            else:
                total_input, total_output, total_requests, queue_depth = await do_get_status()
        except Exception as e:
            logger.warning("Redis error getting status: %s", e)
            total_input = 0
            total_output = 0
            total_requests = 0
            queue_depth = 0

        return RateLimitStatus(
            model=self.model_name,
            window_seconds=self.window_seconds,
            tokens_used=total_input + total_output,
            tokens_limit=self.tpm_limit,
            input_tokens_used=total_input,
            input_tokens_limit=self.input_tpm_limit,
            output_tokens_used=total_output,
            output_tokens_limit=self.output_tpm_limit,
            requests_used=total_requests,
            requests_limit=self.rpm_limit,
            queue_depth=queue_depth,
        )

    async def _execute_acquire(
        self,
        input_tokens: int,
        output_tokens: int,
    ) -> AcquireResult:
        """Execute the acquire operation with the Lua script.

        Args:
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens.

        Returns:
            AcquireResult with slot time, wait time, queue position, and record ID.
        """
        current_time = time.time()
        record_id = str(uuid.uuid4())

        async def do_acquire() -> tuple[float, int, str, float]:
            result = await self.redis.eval(  # type: ignore[misc]
                self._acquire_script,
                1,  # number of keys
                self.consumption_key,
                input_tokens,
                output_tokens,
                self.tpm_limit,  # combined limit (0 = disabled)
                self.input_tpm_limit,  # input limit (0 = disabled)
                self.output_tpm_limit,  # output limit (0 = disabled)
                self.rpm_limit,  # request limit (0 = disabled)
                self.window_seconds,
                current_time,
                record_id,
            )
            return (
                float(result[0]),
                int(result[1]),
                str(result[2]),
                float(result[3]),
            )

        try:
            if self._retry_config is not None:
                slot_time, queue_position, returned_record_id, wait_time = await retry_with_backoff(
                    do_acquire, self._retry_config, "acquire"
                )
            else:
                slot_time, queue_position, returned_record_id, wait_time = await do_acquire()

            # Wait if needed
            if self._should_wait and wait_time > 0:
                logger.debug(
                    "Rate limited: waiting %.2fs (queue position %d)",
                    wait_time,
                    queue_position,
                )
                await asyncio.sleep(wait_time)

            return AcquireResult(
                slot_time=slot_time,
                wait_time=wait_time,
                queue_position=queue_position,
                record_id=returned_record_id,
            )

        except Exception as e:
            # Graceful degradation - allow request on Redis failure
            logger.warning("Redis error, allowing request: %s", e)
            return AcquireResult(
                slot_time=current_time,
                wait_time=0.0,
                queue_position=0,
                record_id=record_id,
            )
