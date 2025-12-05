# API Reference

Complete API documentation for LLMRateLimiter.

## Main Module

The main module exports all public classes and functions.

::: llmratelimiter
    options:
      show_root_heading: false
      show_source: false
      members:
        - RateLimiter
        - RateLimitConfig
        - RedisConnectionManager
        - RetryConfig
        - AcquireResult
        - RateLimitStatus

## Configuration

Configuration dataclasses for rate limits and retry behavior.

::: llmratelimiter.config
    options:
      show_root_heading: false
      show_source: true

## Connection Management

Redis connection pooling and retry logic.

::: llmratelimiter.connection
    options:
      show_root_heading: false
      show_source: true

## Rate Limiter

The main rate limiter implementation.

::: llmratelimiter.limiter
    options:
      show_root_heading: false
      show_source: true
      members:
        - RateLimiter

## Models

Data models for results and status.

::: llmratelimiter.models
    options:
      show_root_heading: false
      show_source: true
