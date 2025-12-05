"""Result dataclasses for rate limiter operations."""

from dataclasses import dataclass


@dataclass
class AcquireResult:
    """Result from an acquire() call.

    Attributes:
        slot_time: The timestamp when the request is scheduled to execute.
        wait_time: Time in seconds the caller waited (or will wait).
        queue_position: Position in the FIFO queue (0 if immediate).
        record_id: Unique ID for this consumption record (for adjust()).
    """

    slot_time: float
    wait_time: float
    queue_position: int
    record_id: str


@dataclass
class RateLimitStatus:
    """Current status of a rate limiter.

    Unified status for both combined and split mode limiters.
    Unused fields are set to 0.

    Combined mode (tpm > 0):
        - tokens_used/tokens_limit contain combined token usage
        - input_tokens_used/input_tokens_limit are 0
        - output_tokens_used/output_tokens_limit are 0

    Split mode (input_tpm/output_tpm > 0):
        - tokens_used/tokens_limit are 0
        - input_tokens_used/input_tokens_limit contain input token usage
        - output_tokens_used/output_tokens_limit contain output token usage

    Attributes:
        model: The model name this limiter is for.
        window_seconds: The sliding window duration.
        tokens_used: Current combined tokens consumed (combined mode).
        tokens_limit: Maximum combined tokens allowed (combined mode).
        input_tokens_used: Current input tokens consumed (split mode).
        input_tokens_limit: Maximum input tokens allowed (split mode).
        output_tokens_used: Current output tokens consumed (split mode).
        output_tokens_limit: Maximum output tokens allowed (split mode).
        requests_used: Current requests in the window.
        requests_limit: Maximum requests allowed per window.
        queue_depth: Number of pending requests (slot_time > now).
    """

    model: str
    window_seconds: int
    tokens_used: int = 0
    tokens_limit: int = 0
    input_tokens_used: int = 0
    input_tokens_limit: int = 0
    output_tokens_used: int = 0
    output_tokens_limit: int = 0
    requests_used: int = 0
    requests_limit: int = 0
    queue_depth: int = 0


# Backwards compatibility alias
SplitRateLimitStatus = RateLimitStatus
