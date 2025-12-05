# Usage Guide

This guide covers all the ways to use LLMRateLimiter for different LLM providers and configurations.

## Rate Limiting Modes

LLMRateLimiter supports three modes based on how your LLM provider counts tokens:

| Mode | Use Case | Configuration |
|------|----------|---------------|
| **Combined** | OpenAI, Anthropic | `tpm=100000` |
| **Split** | GCP Vertex AI | `input_tpm=4000000, output_tpm=128000` |
| **Mixed** | Custom setups | Both combined and split limits |

## Combined TPM Mode

Use this mode for providers like OpenAI and Anthropic that have a single tokens-per-minute limit.

```python
import asyncio
from redis.asyncio import Redis
from llmratelimiter import RateLimiter, RateLimitConfig

async def main():
    redis = Redis(host="localhost", port=6379)

    # OpenAI GPT-4 limits
    config = RateLimitConfig(
        tpm=100_000,      # 100K tokens per minute
        rpm=100,          # 100 requests per minute
        window_seconds=60 # 1 minute sliding window
    )
    limiter = RateLimiter(redis, "gpt-4", config)

    # Acquire capacity - blocks if rate limited
    result = await limiter.acquire(tokens=5000)
    print(f"Wait time: {result.wait_time:.2f}s, Queue position: {result.queue_position}")

    # Now safe to make API call
    # response = await openai.chat.completions.create(...)

asyncio.run(main())
```

### AcquireResult

The `acquire()` method returns an `AcquireResult` with:

- `slot_time`: When the request was scheduled
- `wait_time`: How long the caller waited (0 if no wait)
- `queue_position`: Position in queue (0 = immediate)
- `record_id`: Unique ID for this consumption record

## Split TPM Mode

Use this mode for providers like GCP Vertex AI that have separate limits for input and output tokens.

```python
from llmratelimiter import RateLimiter, RateLimitConfig

# GCP Vertex AI Gemini 1.5 Pro limits
config = RateLimitConfig(
    input_tpm=4_000_000,   # 4M input tokens per minute
    output_tpm=128_000,    # 128K output tokens per minute
    rpm=360,               # 360 requests per minute
)
limiter = RateLimiter(redis, "gemini-1.5-pro", config)

# Estimate output tokens upfront
result = await limiter.acquire(input_tokens=5000, output_tokens=2048)

# Make API call
response = await vertex_ai.generate(...)

# Adjust with actual output tokens
await limiter.adjust(result.record_id, actual_output=response.output_tokens)
```

### Why Adjust?

When using split mode, you must estimate output tokens before the call. After the call completes, use `adjust()` to correct the estimate:

- If actual < estimated: Frees up capacity for other requests
- If actual > estimated: Uses additional capacity retroactively

## Mixed Mode

You can combine both TPM limits for complex scenarios:

```python
config = RateLimitConfig(
    tpm=500_000,           # Combined limit
    input_tpm=4_000_000,   # Input-specific limit
    output_tpm=128_000,    # Output-specific limit
    rpm=360,
)
limiter = RateLimiter(redis, "custom-model", config)

# All three limits are checked independently
result = await limiter.acquire(input_tokens=5000, output_tokens=2048)
```

## Connection Management

For production use, use `RedisConnectionManager` for automatic connection pooling and retry on transient failures.

### Basic Connection Manager

```python
from llmratelimiter import RedisConnectionManager, RateLimiter, RateLimitConfig

manager = RedisConnectionManager(
    host="localhost",
    port=6379,
    db=0,
    max_connections=10,
)

config = RateLimitConfig(tpm=100_000, rpm=100)
limiter = RateLimiter(manager, "gpt-4", config)
```

### With Retry Configuration

```python
from llmratelimiter import RedisConnectionManager, RetryConfig

manager = RedisConnectionManager(
    host="redis.example.com",
    port=6379,
    password="secret",
    retry_config=RetryConfig(
        max_retries=5,        # Retry up to 5 times
        base_delay=0.1,       # Start with 100ms delay
        max_delay=10.0,       # Cap at 10 seconds
        exponential_base=2.0, # Double delay each retry
        jitter=0.1,           # Add ±10% randomness
    ),
)
```

### Context Manager

Use the connection manager as an async context manager for automatic cleanup:

```python
async with RedisConnectionManager(host="localhost") as manager:
    limiter = RateLimiter(manager, "gpt-4", config)
    await limiter.acquire(tokens=5000)
# Connection pool automatically closed
```

## Error Handling and Graceful Degradation

LLMRateLimiter is designed to fail open - if Redis is unavailable, requests are allowed through rather than blocking indefinitely.

### Retryable vs Non-Retryable Errors

**Retryable** (automatic retry with backoff):
- `ConnectionError` - Network issues
- `TimeoutError` - Redis timeout
- `BusyLoadingError` - Redis loading data

**Non-Retryable** (fail immediately):
- `AuthenticationError` - Wrong password
- `ResponseError` - Script errors

### Example with Error Handling

```python
from llmratelimiter import RateLimiter, RateLimitConfig

config = RateLimitConfig(tpm=100_000, rpm=100)
limiter = RateLimiter(manager, "gpt-4", config)

# Even if Redis fails, this returns immediately with a valid result
result = await limiter.acquire(tokens=5000)

if result.wait_time == 0 and result.queue_position == 0:
    # Either no rate limiting needed, or Redis was unavailable
    pass

# Safe to proceed with API call
response = await openai.chat.completions.create(...)
```

## Monitoring

Use `get_status()` to monitor current rate limit usage:

```python
status = await limiter.get_status()

print(f"Model: {status.model}")
print(f"Tokens used: {status.tokens_used}/{status.tokens_limit}")
print(f"Input tokens: {status.input_tokens_used}/{status.input_tokens_limit}")
print(f"Output tokens: {status.output_tokens_used}/{status.output_tokens_limit}")
print(f"Requests: {status.requests_used}/{status.requests_limit}")
print(f"Queue depth: {status.queue_depth}")
```

## Configuration Reference

### RateLimitConfig

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `tpm` | int | 0 | Combined tokens-per-minute limit |
| `input_tpm` | int | 0 | Input tokens-per-minute limit |
| `output_tpm` | int | 0 | Output tokens-per-minute limit |
| `rpm` | int | 0 | Requests-per-minute limit |
| `window_seconds` | int | 60 | Sliding window duration |
| `burst_multiplier` | float | 1.0 | Multiply limits for burst allowance |

### RetryConfig

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_retries` | int | 3 | Maximum retry attempts |
| `base_delay` | float | 0.1 | Initial delay (seconds) |
| `max_delay` | float | 5.0 | Maximum delay cap (seconds) |
| `exponential_base` | float | 2.0 | Backoff multiplier |
| `jitter` | float | 0.1 | Random variation (0-1) |

### RedisConnectionManager

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `host` | str | "localhost" | Redis host |
| `port` | int | 6379 | Redis port |
| `db` | int | 0 | Redis database number |
| `password` | str | None | Redis password |
| `max_connections` | int | 10 | Connection pool size |
| `retry_config` | RetryConfig | RetryConfig() | Retry configuration |
