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

    AWS Bedrock with burndown rate (output tokens count 5x):
        >>> limiter = RateLimiter("redis://localhost", "claude-sonnet",
        ...                       tpm=100_000, rpm=100, burndown_rate=5.0)
        >>> await limiter.acquire(input_tokens=3000, output_tokens=1000)
        # TPM consumption: 3000 + (5.0 * 1000) = 8000 tokens

    Azure OpenAI with RPS smoothing (burst prevention):
        >>> limiter = RateLimiter("redis://localhost", "gpt-4",
        ...                       tpm=300_000, rpm=600, smooth_requests=True)
        # Auto-calculates RPS = 600/60 = 10, enforces 100ms minimum gap

        >>> limiter = RateLimiter("redis://localhost", "gpt-4",
        ...                       tpm=300_000, rpm=600, rps=8)
        # Explicit RPS, auto-enables smoothing, enforces 125ms minimum gap
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
        burndown_rate: float = 1.0,
        smooth_requests: bool = True,
        rps: int = 0,
        smoothing_interval: float = 1.0,
        # Redis connection kwargs (for URL connections)
        password: str | None = None,
        db: int = 0,
        max_connections: int = 10,
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
            burndown_rate: Output token multiplier for combined TPM (default 1.0).
                AWS Bedrock Claude models use 5.0.
            smooth_requests: Enable RPS smoothing to prevent burst-triggered rate limits.
                When True, auto-calculates RPS from RPM. Default True.
            rps: Explicit requests-per-second limit. When set > 0, auto-enables smoothing.
                Set to 0 to auto-calculate from RPM when smooth_requests=True.
            smoothing_interval: Evaluation window in seconds for RPS enforcement.
                Azure uses 1.0s intervals. Default 1.0.
            password: Redis password (for URL connections).
            db: Redis database number (for URL connections).
            max_connections: Maximum connections in pool (for URL connections).
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
                burndown_rate=burndown_rate,
                smooth_requests=smooth_requests,
                rps=rps,
                smoothing_interval=smoothing_interval,
            )

        self.window_seconds = config.window_seconds
        self.burst_multiplier = config.burst_multiplier
        self._burndown_rate = config.burndown_rate
        self._config = config

        # Calculate effective limits with burst multiplier
        self.rpm_limit = int(config.rpm * config.burst_multiplier) if config.rpm > 0 else 0
        self.tpm_limit = int(config.tpm * config.burst_multiplier) if config.tpm > 0 else 0
        self.input_tpm_limit = int(config.input_tpm * config.burst_multiplier) if config.input_tpm > 0 else 0
        self.output_tpm_limit = int(config.output_tpm * config.burst_multiplier) if config.output_tpm > 0 else 0

        # RPS smoothing settings
        self._rps_limit = config.effective_rps
        self._smoothing_interval = config.smoothing_interval

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

        For combined mode with pre-calculated tokens, use tokens parameter:
            await limiter.acquire(tokens=5000)
            # Burndown rate is NOT applied - value is used directly

        For separate input/output tracking, use input_tokens/output_tokens:
            await limiter.acquire(input_tokens=5000, output_tokens=2048)
            # Burndown rate IS applied: effective = input + (burndown_rate * output)

        With burndown rate (e.g., AWS Bedrock with burndown_rate=5.0):
            await limiter.acquire(input_tokens=3000, output_tokens=1000)
            # TPM consumption: 3000 + (5.0 * 1000) = 8000 tokens

        Blocks until capacity is available (FIFO ordering), then returns.
        On Redis failure (after retries if configured), allows the request
        (graceful degradation).

        Note: The burndown_rate is only applied when using input_tokens/output_tokens.
        When using the tokens= parameter, it is assumed the burndown calculation
        has already been done by the caller. Split input/output TPM limits
        are not affected by burndown_rate.

        Args:
            tokens: Pre-calculated total tokens (burndown already applied if needed).
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens (default 0).

        Returns:
            AcquireResult with slot time, wait time, queue position, and record ID.
        """
        # Resolve input tokens and determine if burndown rate should be applied
        if tokens is not None:
            if input_tokens is not None:
                raise ValueError("Cannot specify both tokens and input_tokens")
            # When tokens= is used, assume burndown is already applied
            # Use the value directly as effective_combined_tokens
            input_tokens = tokens
            effective_combined_tokens = float(tokens)
        else:
            if input_tokens is None:
                raise ValueError("Must specify either tokens or input_tokens")
            # When input_tokens/output_tokens are used, apply burndown rate
            effective_combined_tokens = input_tokens + (self._burndown_rate * output_tokens)

        return await self._execute_acquire(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            effective_combined_tokens=effective_combined_tokens,
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
        effective_combined_tokens: float,
    ) -> AcquireResult:
        """Execute the acquire operation with the Lua script.

        Args:
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens.
            effective_combined_tokens: Pre-calculated combined tokens (with burndown rate if applicable).

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
                effective_combined_tokens,  # pre-calculated with burndown rate
                self._rps_limit,  # RPS limit (0 = disabled)
                self._smoothing_interval,  # smoothing interval in seconds
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
