import os
from dataclasses import dataclass

# data.gov.my allows 4 requests/minute per API. The worker makes one realtime request
# per tick, so a 15 s minimum gap between tick starts guarantees we stay within it.
MIN_POLL_SECONDS = 60 / 4


@dataclass(frozen=True)
class Settings:
    poll_seconds: float = 30
    max_backoff_seconds: float = 300
    stale_after_seconds: float = 300  # data older than this = unhealthy / skipped
    retention_days: int = 14

    def __post_init__(self) -> None:
        if self.poll_seconds < MIN_POLL_SECONDS:
            raise ValueError(
                f"POLL_SECONDS={self.poll_seconds} would exceed 4 requests/min; "
                f"minimum is {MIN_POLL_SECONDS}"
            )
        if self.retention_days < 1:
            raise ValueError("RETENTION_DAYS must be at least 1")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            poll_seconds=float(os.getenv("POLL_SECONDS", cls.poll_seconds)),
            max_backoff_seconds=float(os.getenv("MAX_BACKOFF_SECONDS", cls.max_backoff_seconds)),
            stale_after_seconds=float(os.getenv("STALE_AFTER_SECONDS", cls.stale_after_seconds)),
            retention_days=int(os.getenv("RETENTION_DAYS", cls.retention_days)),
        )
