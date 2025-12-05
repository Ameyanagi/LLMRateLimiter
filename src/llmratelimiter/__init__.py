"""LLM Rate Limiter - Client-side rate limiting for LLM API calls.

This library provides FIFO queue-based rate limiting to prevent hitting
provider rate limits (TPM/RPM) when calling LLM APIs.

Basic usage (raw Redis client):
    >>> from llmratelimiter import RateLimiter, RateLimitConfig
    >>> from redis.asyncio import Redis
    >>>
    >>> redis = Redis(host="localhost", port=6379)
    >>> config = RateLimitConfig(tpm=100_000, rpm=100)
    >>> limiter = RateLimiter(redis, "gpt-4", config)
    >>>
    >>> await limiter.acquire(tokens=5000)
    >>> response = await openai.chat.completions.create(...)

With connection manager (includes retry with exponential backoff):
    >>> from llmratelimiter import (
    ...     RateLimiter, RateLimitConfig, RedisConnectionManager, RetryConfig
    ... )
    >>>
    >>> manager = RedisConnectionManager(
    ...     host="localhost",
    ...     port=6379,
    ...     retry_config=RetryConfig(max_retries=3, base_delay=0.1),
    ... )
    >>> config = RateLimitConfig(tpm=100_000, rpm=100)
    >>> limiter = RateLimiter(manager, "gpt-4", config)
    >>>
    >>> await limiter.acquire(tokens=5000)

Split mode example (GCP Vertex AI):
    >>> config = RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)
    >>> limiter = RateLimiter(manager, "gemini-1.5-pro", config)
    >>>
    >>> result = await limiter.acquire(input_tokens=5000, output_tokens=2048)
    >>> response = await vertex_ai.generate(...)
    >>> await limiter.adjust(result.record_id, actual_output=response.output_tokens)
"""

from llmratelimiter.config import RateLimitConfig, RetryConfig
from llmratelimiter.connection import RedisConnectionManager
from llmratelimiter.limiter import RateLimiter
from llmratelimiter.models import AcquireResult, RateLimitStatus

__all__ = [
    # Main classes
    "RateLimiter",
    "RateLimitConfig",
    # Connection management
    "RedisConnectionManager",
    "RetryConfig",
    # Result types
    "AcquireResult",
    "RateLimitStatus",
]

__version__ = "0.1.0"
