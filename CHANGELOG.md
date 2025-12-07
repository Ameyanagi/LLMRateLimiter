# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
