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

    Burndown rate (AWS Bedrock):
        RateLimitConfig(tpm=100_000, rpm=100, burndown_rate=5.0)
        # TPM consumption = input_tokens + (burndown_rate * output_tokens)

    RPS smoothing (Azure OpenAI burst prevention):
        RateLimitConfig(tpm=300_000, rpm=600, smooth_requests=True)
        # Auto-calculates RPS = 600/60 = 10, enforces 100ms minimum gap

        RateLimitConfig(tpm=300_000, rpm=600, rps=8)
        # Explicit RPS, auto-enables smoothing, enforces 125ms minimum gap

    Args:
        rpm: Requests per minute limit. Set to 0 to disable.
        tpm: Combined tokens per minute limit (input + output). Set to 0 to disable.
        input_tpm: Input tokens per minute limit. Set to 0 to disable.
        output_tpm: Output tokens per minute limit. Set to 0 to disable.
        window_seconds: Sliding window duration in seconds.
        burst_multiplier: Multiplier for burst capacity above base limits.
        burndown_rate: Output token multiplier for combined TPM (default 1.0).
            AWS Bedrock Claude models use 5.0.
        smooth_requests: Enable RPS smoothing to prevent burst-triggered rate limits.
            When True, auto-calculates RPS from RPM. Default False.
        rps: Explicit requests-per-second limit. When set > 0, auto-enables smoothing.
            Set to 0 to auto-calculate from RPM when smooth_requests=True.
        smoothing_interval: Evaluation window in seconds for RPS enforcement.
            Azure uses 1.0s intervals. Default 1.0.
    """

    rpm: int
    tpm: int = 0
    input_tpm: int = 0
    output_tpm: int = 0
    window_seconds: int = 60
    burst_multiplier: float = 1.0
    burndown_rate: float = 1.0
    smooth_requests: bool = False
    rps: int = 0
    smoothing_interval: float = 1.0

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if self.burndown_rate < 0:
            raise ValueError("burndown_rate must be >= 0")
        if self.rps < 0:
            raise ValueError("rps must be >= 0")
        if self.smoothing_interval <= 0:
            raise ValueError("smoothing_interval must be > 0")

    @property
    def is_split_mode(self) -> bool:
        """Whether this config uses split input/output TPM limits."""
        return self.input_tpm > 0 or self.output_tpm > 0

    @property
    def has_combined_limit(self) -> bool:
        """Whether this config has a combined TPM limit."""
        return self.tpm > 0

    @property
    def is_smoothing_enabled(self) -> bool:
        """Whether RPS smoothing is active.

        Smoothing is enabled when either:
        - smooth_requests=True (auto-calculate RPS from RPM)
        - rps > 0 (explicit RPS, auto-enables smoothing)
        """
        return self.rps > 0 or self.smooth_requests

    @property
    def effective_rps(self) -> float:
        """Calculate effective RPS limit.

        Returns:
            Explicit rps if set, otherwise rpm/60 if smoothing enabled, else 0.
        """
        if self.rps > 0:
            return float(self.rps)
        if self.smooth_requests and self.rpm > 0:
            return self.rpm / 60.0
        return 0.0
