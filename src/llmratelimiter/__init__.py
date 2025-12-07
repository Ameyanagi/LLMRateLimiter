"""LLM Rate Limiter - Client-side rate limiting for LLM API calls.

This library provides FIFO queue-based rate limiting to prevent hitting
provider rate limits (TPM/RPM) when calling LLM APIs.

Basic usage (recommended: specify input and output tokens separately):
    >>> from llmratelimiter import RateLimiter
    >>>
    >>> limiter = RateLimiter("redis://localhost:6379", "gpt-4", tpm=100_000, rpm=100)
    >>> await limiter.acquire(input_tokens=3000, output_tokens=2000)
    >>> response = await openai.chat.completions.create(...)

With existing Redis client:
    >>> from llmratelimiter import RateLimiter
    >>> from redis.asyncio import Redis
    >>>
    >>> redis = Redis(host="localhost", port=6379)
    >>> limiter = RateLimiter(redis=redis, model="gpt-4", tpm=100_000, rpm=100)
    >>> await limiter.acquire(input_tokens=3000, output_tokens=2000)

With connection manager (includes retry with exponential backoff):
    >>> from llmratelimiter import RateLimiter, RedisConnectionManager, RetryConfig
    >>>
    >>> manager = RedisConnectionManager(
    ...     "redis://localhost:6379",
    ...     retry_config=RetryConfig(max_retries=3, base_delay=0.1),
    ... )
    >>> limiter = RateLimiter(manager, "gpt-4", tpm=100_000, rpm=100)
    >>> await limiter.acquire(input_tokens=3000, output_tokens=2000)

Split mode example (GCP Vertex AI):
    >>> limiter = RateLimiter(
    ...     "redis://localhost:6379", "gemini-1.5-pro",
    ...     input_tpm=4_000_000, output_tpm=128_000, rpm=360
    ... )
    >>> result = await limiter.acquire(input_tokens=5000, output_tokens=2048)
    >>> response = await vertex_ai.generate(...)
    >>> await limiter.adjust(result.record_id, actual_output=response.output_tokens)

AWS Bedrock with burndown rate (output tokens count 5x toward TPM):
    >>> limiter = RateLimiter(
    ...     "redis://localhost:6379", "claude-sonnet",
    ...     tpm=100_000, rpm=100, burndown_rate=5.0
    ... )
    >>> await limiter.acquire(input_tokens=3000, output_tokens=1000)
    # TPM consumption: 3000 + (5.0 * 1000) = 8000 tokens
"""

from llmratelimiter.config import RateLimitConfig, RetryConfig
from llmratelimiter.connection import RedisConnectionManager
from llmratelimiter.limiter import RateLimiter
from llmratelimiter.models import AcquireResult, RateLimitStatus

__all__ = [
    "AcquireResult",
    "RateLimitConfig",
    "RateLimitStatus",
    "RateLimiter",
    "RedisConnectionManager",
    "RetryConfig",
]

__version__ = "0.2.0"
