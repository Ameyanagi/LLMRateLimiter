# LLMRateLimiter

[![Release](https://img.shields.io/github/v/release/Ameyanagi/LLMRateLimiter)](https://img.shields.io/github/v/release/Ameyanagi/LLMRateLimiter)
[![Build status](https://img.shields.io/github/actions/workflow/status/Ameyanagi/LLMRateLimiter/main.yml?branch=main)](https://github.com/Ameyanagi/LLMRateLimiter/actions/workflows/main.yml?query=branch%3Amain)
[![License](https://img.shields.io/github/license/Ameyanagi/LLMRateLimiter)](https://img.shields.io/github/license/Ameyanagi/LLMRateLimiter)

**Client-side rate limiting for LLM API calls using Redis-backed FIFO queues.**

LLMRateLimiter helps you stay within your LLM provider's rate limits (TPM/RPM) by coordinating requests across multiple processes or servers using Redis.

## Key Features

- **FIFO Queue-Based**: Requests are processed in order, preventing thundering herd problems
- **Distributed**: Redis-backed for multi-process and multi-server deployments
- **Flexible Limits**: Supports combined TPM (OpenAI/Anthropic), split input/output TPM (GCP Vertex AI), or both
- **Automatic Retry**: Exponential backoff with jitter for transient Redis failures
- **Graceful Degradation**: Allows requests through when Redis is unavailable

## Installation

```bash
pip install llmratelimiter
```

Or with uv:

```bash
uv add llmratelimiter
```

## Quickstart

### Simple Usage

```python
from llmratelimiter import RateLimiter

# Just pass a Redis URL and your limits
limiter = RateLimiter("redis://localhost:6379", "gpt-4", tpm=100_000, rpm=100)

await limiter.acquire(tokens=5000)
response = await openai.chat.completions.create(...)
```

### Split Mode (GCP Vertex AI)

```python
from llmratelimiter import RateLimiter

# Separate input/output token limits
limiter = RateLimiter(
    "redis://localhost:6379", "gemini-1.5-pro",
    input_tpm=4_000_000, output_tpm=128_000, rpm=360
)

# Estimate output tokens upfront
result = await limiter.acquire(input_tokens=5000, output_tokens=2048)
response = await vertex_ai.generate(...)

# Adjust after getting actual output tokens
await limiter.adjust(result.record_id, actual_output=response.output_tokens)
```

### With Existing Redis Client

```python
from redis.asyncio import Redis
from llmratelimiter import RateLimiter

redis = Redis(host="localhost", port=6379)
limiter = RateLimiter(redis=redis, model="gpt-4", tpm=100_000, rpm=100)

await limiter.acquire(tokens=5000)
```

### Production Setup with Retry

```python
from llmratelimiter import RateLimiter, RedisConnectionManager, RetryConfig

# Use connection manager for automatic retry and pooling
manager = RedisConnectionManager(
    "redis://localhost:6379",
    retry_config=RetryConfig(max_retries=3, base_delay=0.1),
)
limiter = RateLimiter(manager, "gpt-4", tpm=100_000, rpm=100)

await limiter.acquire(tokens=5000)
```

### SSL Connection

```python
from llmratelimiter import RateLimiter

# Use rediss:// for SSL connections
limiter = RateLimiter("rediss://localhost:6379", "gpt-4", tpm=100_000, rpm=100)
```

## Next Steps

- [Usage Guide](usage.md) - Detailed examples for all modes and configurations
- [API Reference](api.md) - Complete API documentation
