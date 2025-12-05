"""Configuration dataclasses for rate limiters."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryConfig:
    """Configuration for retry behavior with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts (0 = no retries).
        base_delay: Initial delay in seconds before first retry.
        max_delay: Maximum delay in seconds between retries.
        exponential_base: Multiplier for exponential backoff (delay * base^attempt).
        jitter: Random jitter factor (0.0 to 1.0) to prevent thundering herd.

    Example:
        >>> config = RetryConfig(max_retries=3, base_delay=0.1)
        # Retry delays: ~0.1s, ~0.2s, ~0.4s (with jitter)
    """

    max_retries: int = 3
    base_delay: float = 0.1
    max_delay: float = 5.0
    exponential_base: float = 2.0
    jitter: float = 0.1

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.base_delay <= 0:
            raise ValueError("base_delay must be > 0")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be >= base_delay")
        if self.exponential_base < 1:
            raise ValueError("exponential_base must be >= 1")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")


@dataclass(frozen=True)
class RateLimitConfig:
    """Unified configuration for rate limiting.

    Supports combined TPM, split TPM, or both. Set unused limits to 0 to disable.

    Combined mode only:
        RateLimitConfig(tpm=100_000, rpm=100)

    Split mode only:
        RateLimitConfig(input_tpm=4_000_000, output_tpm=128_000, rpm=360)

    Mixed mode (all three limits):
        RateLimitConfig(tpm=100_000, input_tpm=80_000, output_tpm=20_000, rpm=100)
        # Request must satisfy ALL constraints

    Disabling limits:
        - Set rpm=0 to disable request rate limiting
        - Set tpm=0 to disable combined token limiting
        - Set input_tpm=0 or output_tpm=0 to disable that specific limit

    Args:
        rpm: Requests per minute limit. Set to 0 to disable.
        tpm: Combined tokens per minute limit (input + output). Set to 0 to disable.
        input_tpm: Input tokens per minute limit. Set to 0 to disable.
        output_tpm: Output tokens per minute limit. Set to 0 to disable.
        window_seconds: Sliding window duration in seconds.
        burst_multiplier: Multiplier for burst capacity above base limits.
    """

    rpm: int
    tpm: int = 0
    input_tpm: int = 0
    output_tpm: int = 0
    window_seconds: int = 60
    burst_multiplier: float = 1.0

    @property
    def is_split_mode(self) -> bool:
        """Whether this config uses split input/output TPM limits."""
        return self.input_tpm > 0 or self.output_tpm > 0

    @property
    def has_combined_limit(self) -> bool:
        """Whether this config has a combined TPM limit."""
        return self.tpm > 0
