# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.2] - 2025-12-18

### Fixed

- Fix RESP2 float truncation: Return floats as strings from Lua script to preserve precision when returning wait_time and slot_time values.
- Fix first request delay: Only apply RPS smoothing when there are existing requests in the window (first request is now immediate).
- Fix `rps=None` TypeError: Handle None values in `RateLimitConfig` validation and properties.
- Update integration tests to use approximate comparison for wait_time to account for epsilon FIFO spacing.

## [0.3.1] - 2025-12-18

### Fixed

- Bug fixes for RPS smoothing and `rps=None` handling.

## [0.3.0] - 2025-12-17

### Added

- **RPS Smoothing**: New feature to prevent burst-triggered 429 errors with Azure OpenAI and other providers that enforce rate limits at sub-second intervals.
  - `smooth_requests`: Enable auto-calculated RPS from RPM (default: `True`)
  - `rps`: Explicit RPS limit (auto-enables smoothing)
  - `smoothing_interval`: Evaluation window in seconds (default: `1.0`)

### Changed

- `smooth_requests` now defaults to `True` to prevent burst issues by default.

## [0.2.0] - 2025-12-07

### Added

- **Burndown Rate Support**: New `burndown_rate` parameter for AWS Bedrock-style rate limiting where output tokens count more heavily toward TPM limits.
  - Default: `1.0` (no change in behavior)
  - AWS Bedrock Claude models: `5.0` (output tokens count 5x)
  - Formula: `effective_tpm = input_tokens + (burndown_rate * output_tokens)`

- **Recommended Usage Pattern**: `acquire(input_tokens=X, output_tokens=Y)` is now the recommended way to call acquire, enabling accurate burndown rate calculations.

### Changed

- Updated all documentation examples to use `input_tokens`/`output_tokens` instead of `tokens=`.
- When using `tokens=` parameter, the value is used directly (assumes burndown already applied by caller).
- When using `input_tokens`/`output_tokens`, the burndown rate is automatically applied.

### Example

```python
# AWS Bedrock with 5x burndown rate
limiter = RateLimiter(
    "redis://localhost:6379", "claude-sonnet",
    tpm=100_000, rpm=100, burndown_rate=5.0
)

await limiter.acquire(input_tokens=3000, output_tokens=1000)
# TPM consumption: 3000 + (5.0 * 1000) = 8000 tokens
```
