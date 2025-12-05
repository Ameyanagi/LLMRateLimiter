# Test Documentation

This page documents all 77 tests in the LLMRateLimiter test suite. Tests are organized by category and each test explanation includes what behavior it validates and why it matters.

## Test Organization

| File | Tests | Description |
|------|-------|-------------|
| `test_combined_limiter.py` | 10 | Combined TPM+RPM limiting (OpenAI/Anthropic) |
| `test_split_limiter.py` | 11 | Split input/output TPM limiting (GCP Vertex AI) |
| `test_connection.py` | 32 | Connection management and retry logic |
| `test_integration.py` | 24 | End-to-end tests with real Redis |

---

## Combined Mode Tests

Tests for the combined TPM+RPM rate limiting mode used by providers like OpenAI and Anthropic.

### TestCombinedModeBasic

#### `test_acquire_under_limits_returns_immediately`
**What it tests**: When token usage is under configured limits, `acquire()` returns immediately with zero wait time.

**Why it matters**: This is the happy path - most requests should not be rate limited when capacity is available.

#### `test_acquire_at_capacity_waits`
**What it tests**: When at capacity, `acquire()` blocks for the calculated wait time before returning.

**Why it matters**: Validates that the rate limiter actually enforces limits by making callers wait when necessary.

#### `test_acquire_returns_queue_position`
**What it tests**: `acquire()` returns the correct queue position indicating how many requests are ahead.

**Why it matters**: Queue position helps callers understand their place in line and estimate wait times.

### TestCombinedModeConfig

#### `test_burst_multiplier_applied`
**What it tests**: The `burst_multiplier` configuration correctly increases effective limits (e.g., 1.5x multiplier on 100K TPM = 150K effective limit).

**Why it matters**: Burst multiplier allows temporary spikes above base limits for bursty workloads.

#### `test_custom_window_seconds`
**What it tests**: Custom `window_seconds` configuration is properly applied to the limiter.

**Why it matters**: Different use cases may need different sliding window durations (e.g., 30s vs 120s).

#### `test_is_split_mode_false_for_combined`
**What it tests**: The `is_split_mode` property returns `False` for combined-only configurations.

**Why it matters**: Allows code to detect which mode is active and adjust behavior accordingly.

### TestCombinedModeGracefulDegradation

#### `test_redis_error_allows_request`
**What it tests**: When Redis fails, `acquire()` returns immediately with zero wait time instead of blocking.

**Why it matters**: Graceful degradation prevents Redis outages from blocking all LLM API calls.

### TestCombinedModeStatus

#### `test_get_status_returns_correct_info`
**What it tests**: `get_status()` returns accurate usage information including tokens used, requests used, and queue depth.

**Why it matters**: Status monitoring is essential for dashboards and alerting on rate limit usage.

### TestCombinedModeLuaScript

#### `test_lua_script_receives_correct_arguments`
**What it tests**: The Lua script receives correct arguments including key name, token count, and limits.

**Why it matters**: Ensures the Python code correctly marshals data to the Redis Lua script.

---

## Split Mode Tests

Tests for the split input/output TPM rate limiting mode used by providers like GCP Vertex AI.

### TestSplitModeBasic

#### `test_acquire_under_limits_returns_immediately`
**What it tests**: When both input and output token usage are under limits, `acquire()` returns immediately.

**Why it matters**: Validates that split mode works correctly when capacity is available.

#### `test_acquire_with_default_output_tokens`
**What it tests**: `output_tokens` parameter defaults to 0 if not specified.

**Why it matters**: Simplifies API for cases where output tokens are unknown or zero.

### TestSplitModeConfig

#### `test_burst_multiplier_applied_to_all_limits`
**What it tests**: Burst multiplier is applied to input TPM, output TPM, and RPM limits.

**Why it matters**: Ensures consistent burst behavior across all limit types.

#### `test_is_split_mode_true_for_split`
**What it tests**: The `is_split_mode` property returns `True` for split configurations.

**Why it matters**: Allows code to detect split mode and enable adjust() functionality.

### TestSplitModeAdjust

#### `test_adjust_updates_output_tokens`
**What it tests**: `adjust()` successfully updates the output tokens for a consumption record.

**Why it matters**: Core functionality for correcting output token estimates after API response.

#### `test_adjust_handles_not_found`
**What it tests**: `adjust()` handles missing records gracefully without raising errors.

**Why it matters**: Records may expire before adjust is called; this shouldn't crash the application.

#### `test_adjust_works_in_combined_mode`
**What it tests**: `adjust()` can be called in combined mode (useful for tracking actual usage).

**Why it matters**: Provides flexibility - adjust() isn't restricted to split mode only.

### TestSplitModeStatus

#### `test_get_status_returns_split_info`
**What it tests**: `get_status()` returns separate input and output token usage in split mode.

**Why it matters**: Monitoring needs to show both input and output consumption separately.

### TestSplitModeGracefulDegradation

#### `test_redis_error_allows_request`
**What it tests**: Redis errors in split mode also trigger graceful degradation.

**Why it matters**: Ensures fail-open behavior works consistently across all modes.

### TestSplitModeLuaScript

#### `test_lua_script_receives_both_token_types`
**What it tests**: Lua script receives both input_tokens and output_tokens as separate arguments.

**Why it matters**: Validates correct data marshaling for split mode.

---

## Connection Management Tests

Tests for Redis connection pooling, retry logic, and exponential backoff.

### TestRetryConfig

#### `test_default_values`
**What it tests**: RetryConfig has sensible defaults (3 retries, 0.1s base delay, 5.0s max delay).

**Why it matters**: Users should get reasonable behavior without configuration.

#### `test_custom_values`
**What it tests**: All RetryConfig parameters can be customized.

**Why it matters**: Different environments need different retry strategies.

#### `test_zero_retries_allowed`
**What it tests**: `max_retries=0` is valid (disables retry).

**Why it matters**: Some users may want fail-fast behavior.

#### `test_negative_retries_rejected`
**What it tests**: Negative `max_retries` raises ValueError.

**Why it matters**: Prevents configuration errors.

#### `test_zero_base_delay_rejected`
**What it tests**: Zero `base_delay` raises ValueError.

**Why it matters**: Zero delay would cause tight retry loops.

#### `test_negative_base_delay_rejected`
**What it tests**: Negative `base_delay` raises ValueError.

**Why it matters**: Negative delays are nonsensical.

#### `test_max_delay_less_than_base_rejected`
**What it tests**: `max_delay < base_delay` raises ValueError.

**Why it matters**: Max should never be less than base.

#### `test_exponential_base_less_than_one_rejected`
**What it tests**: `exponential_base < 1` raises ValueError.

**Why it matters**: Base < 1 would cause delays to decrease over time.

#### `test_jitter_negative_rejected`
**What it tests**: Negative jitter raises ValueError.

**Why it matters**: Jitter must be a positive fraction.

#### `test_jitter_greater_than_one_rejected`
**What it tests**: Jitter > 1.0 raises ValueError.

**Why it matters**: Jitter > 100% could cause negative delays.

#### `test_config_is_frozen`
**What it tests**: RetryConfig is immutable (frozen dataclass).

**Why it matters**: Prevents accidental modification after creation.

### TestCalculateDelay

#### `test_first_attempt_uses_base_delay`
**What it tests**: First retry (attempt 0) uses exactly `base_delay`.

**Why it matters**: Establishes the baseline for exponential growth.

#### `test_exponential_growth`
**What it tests**: Delays grow exponentially: 0.1s → 0.2s → 0.4s → 0.8s.

**Why it matters**: Validates the core exponential backoff algorithm.

#### `test_max_delay_cap`
**What it tests**: Delays are capped at `max_delay` regardless of attempt number.

**Why it matters**: Prevents extremely long waits on many retries.

#### `test_jitter_adds_randomness`
**What it tests**: Jitter adds ±10% variation to delays.

**Why it matters**: Randomization prevents thundering herd on recovery.

#### `test_delay_never_negative`
**What it tests**: Delay is always >= 0 even with maximum jitter.

**Why it matters**: Negative sleep times would cause errors.

### TestRetryWithBackoff

#### `test_success_on_first_attempt`
**What it tests**: Successful operations return immediately without retry.

**Why it matters**: Retry should only activate on failure.

#### `test_success_after_retry`
**What it tests**: Operation succeeds after transient failure and retry.

**Why it matters**: Core retry functionality works correctly.

#### `test_max_retries_exceeded`
**What it tests**: After exhausting retries, the last exception is raised.

**Why it matters**: Retries must eventually give up.

#### `test_non_retryable_error_raises_immediately`
**What it tests**: ResponseError (Lua script error) is not retried.

**Why it matters**: Some errors are permanent and shouldn't be retried.

#### `test_authentication_error_not_retried`
**What it tests**: AuthenticationError is not retried.

**Why it matters**: Wrong password won't become right with retries.

#### `test_timeout_error_is_retried`
**What it tests**: TimeoutError triggers retry with backoff.

**Why it matters**: Timeouts are transient and often recoverable.

#### `test_busy_loading_error_is_retried`
**What it tests**: BusyLoadingError (Redis loading data) triggers retry.

**Why it matters**: Redis will become available after loading completes.

#### `test_zero_retries_no_retry`
**What it tests**: With `max_retries=0`, failures raise immediately.

**Why it matters**: Validates fail-fast configuration works.

#### `test_exponential_delay_timing`
**What it tests**: Actual sleep times match expected exponential delays.

**Why it matters**: Verifies the backoff timing is correct in practice.

### TestRedisConnectionManager

#### `test_default_values`
**What it tests**: RedisConnectionManager has sensible defaults (localhost:6379, db=0, 10 connections).

**Why it matters**: Works out of the box for local development.

#### `test_custom_values`
**What it tests**: All connection parameters can be customized.

**Why it matters**: Production environments need custom configuration.

#### `test_client_property_creates_pool`
**What it tests**: First access to `.client` creates the connection pool.

**Why it matters**: Lazy initialization avoids unnecessary connections.

#### `test_client_property_reuses_pool`
**What it tests**: Multiple accesses to `.client` return the same instance.

**Why it matters**: Connection pool should be singleton per manager.

#### `test_close_cleans_up`
**What it tests**: `close()` releases client and pool resources.

**Why it matters**: Proper cleanup prevents resource leaks.

#### `test_context_manager`
**What it tests**: `async with` automatically closes on exit.

**Why it matters**: Pythonic resource management pattern.

#### `test_close_idempotent`
**What it tests**: Calling `close()` multiple times is safe.

**Why it matters**: Prevents errors from double-close.

---

## Integration Tests

End-to-end tests with real Redis that validate the complete system.

!!! note "Redis Required"
    These tests require a running Redis instance. They are automatically skipped if Redis is unavailable.

### TestCombinedModeIntegration

#### `test_immediate_acquire_under_limits`
**What it tests**: With real Redis, requests under limits complete immediately.

**Why it matters**: Validates the Lua script works correctly with real Redis.

#### `test_multiple_requests_increment_usage`
**What it tests**: Multiple requests accumulate token and request counts correctly.

**Why it matters**: Verifies consumption tracking works across requests.

#### `test_rpm_limit_causes_wait`
**What it tests**: Exceeding RPM limit causes wait time on subsequent requests.

**Why it matters**: Validates RPM limiting works end-to-end.

#### `test_tpm_limit_causes_wait`
**What it tests**: Exceeding TPM limit causes wait time on subsequent requests.

**Why it matters**: Validates TPM limiting works end-to-end.

#### `test_capacity_freed_after_window`
**What it tests**: After window expires, consumed capacity is freed.

**Why it matters**: Validates sliding window expiration works correctly.

#### `test_fifo_ordering`
**What it tests**: Queued requests get monotonically increasing slot times.

**Why it matters**: Validates FIFO ordering prevents starvation.

### TestSplitModeIntegration

#### `test_immediate_acquire_under_all_limits`
**What it tests**: Split mode requests under all limits complete immediately.

**Why it matters**: Validates split mode Lua script with real Redis.

#### `test_output_tpm_limit_causes_wait`
**What it tests**: Exceeding output TPM limit causes wait time.

**Why it matters**: Validates output-specific limiting works.

#### `test_adjust_updates_record`
**What it tests**: `adjust()` correctly updates output tokens in Redis.

**Why it matters**: Validates the adjust Lua script works correctly.

#### `test_status_shows_split_info`
**What it tests**: Status shows separate input/output token usage from Redis.

**Why it matters**: Validates status Lua script in split mode.

### TestMixedModeIntegration

#### `test_mixed_mode_all_limits_enforced`
**What it tests**: Combined + split limits are all enforced simultaneously.

**Why it matters**: Validates mixed mode works correctly.

#### `test_mixed_mode_combined_limit_triggers_wait`
**What it tests**: Combined limit triggers wait even when split limits are OK.

**Why it matters**: All three limits are checked independently.

#### `test_mixed_mode_input_limit_triggers_wait`
**What it tests**: Input limit triggers wait even when combined limit is OK.

**Why it matters**: Input-specific limiting works in mixed mode.

#### `test_mixed_mode_output_limit_triggers_wait`
**What it tests**: Output limit triggers wait even when other limits are OK.

**Why it matters**: Output-specific limiting works in mixed mode.

#### `test_mixed_mode_status_shows_all_info`
**What it tests**: Status shows combined, input, and output usage.

**Why it matters**: Complete visibility in mixed mode.

### TestConfigValidation

#### `test_mixed_mode_config_is_valid`
**What it tests**: Configs with both tpm and input_tpm/output_tpm are valid.

**Why it matters**: Mixed mode is explicitly supported.

#### `test_combined_mode_config`
**What it tests**: Combined-only config sets correct mode flags.

**Why it matters**: Mode detection works correctly.

#### `test_split_mode_config`
**What it tests**: Split-only config sets correct mode flags.

**Why it matters**: Mode detection works correctly.

#### `test_rpm_only_config`
**What it tests**: RPM-only config (no TPM limits) is valid.

**Why it matters**: Some use cases only need request limiting.

### TestDisabledLimits

#### `test_disabled_rpm_allows_unlimited_requests`
**What it tests**: With `rpm=0`, unlimited requests are allowed.

**Why it matters**: Validates limit disabling works correctly.

### TestConcurrentRequests

#### `test_concurrent_requests_get_unique_positions`
**What it tests**: Concurrent requests all get unique queue positions and record IDs.

**Why it matters**: Redis Lua scripts handle concurrency correctly.

### TestLongWaitTimes

#### `test_wait_time_calculation_for_deep_queue`
**What it tests**: Deep queue correctly calculates long wait times (>4 minutes for 5 requests at full capacity).

**Why it matters**: Wait time calculation is correct even for long queues.

### TestConnectionManagerIntegration

#### `test_limiter_with_connection_manager`
**What it tests**: RateLimiter works correctly with RedisConnectionManager.

**Why it matters**: Validates the connection manager integration.

#### `test_connection_manager_ping`
**What it tests**: Connection manager can ping Redis successfully.

**Why it matters**: Basic connectivity verification.

#### `test_multiple_limiters_share_manager`
**What it tests**: Multiple RateLimiter instances can share one connection manager.

**Why it matters**: Efficient connection pooling across limiters.

#### `test_context_manager_usage`
**What it tests**: Connection manager works as async context manager in integration.

**Why it matters**: Real-world usage pattern validation.

---

## Running Tests

### Run All Tests

```bash
uv run pytest
```

### Run Specific Category

```bash
# Combined mode only
uv run pytest tests/test_combined_limiter.py

# Integration tests (requires Redis)
uv run pytest tests/test_integration.py
```

### Run with Coverage

```bash
uv run pytest --cov --cov-report=html
```

### Skip Integration Tests

If Redis is unavailable, integration tests are automatically skipped.
