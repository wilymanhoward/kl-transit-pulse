"""Long-running ingestion worker: poll realtime every 30 s, refresh the timetable daily,
delete old positions hourly. Every job is wrapped so one failure never stops the loop.

Run: python -m app.worker
"""

import logging
import signal
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from threading import Event
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import Engine, func, select

from app.config import Settings
from app.db import make_engine
from app.gtfs_static import download as download_static
from app.gtfs_static import import_static
from app.logging_setup import setup_logging
from app.models import IngestionRun
from app.realtime import (
    FetchError,
    delete_old_positions,
    fetch_feed,
    ingest_once,
    last_feed_timestamp,
)

log = logging.getLogger(__name__)

KL = ZoneInfo("Asia/Kuala_Lumpur")
STATIC_REFRESH_HOUR = 4  # data.gov.my recommends refreshing static GTFS daily around 04:00
STATIC_RETRY_SECONDS = 15 * 60  # after a failed timetable load, don't retry every tick
RETENTION_EVERY_SECONDS = 60 * 60


def next_delay(
    failures: int, settings: Settings, elapsed: float = 0.0, retry_after: float | None = None
) -> float:
    """Seconds to wait before the next poll.

    Healthy: keep a steady cadence (poll_seconds between tick *starts*).
    After n consecutive failures: poll_seconds * 2**n, capped, but never less than the
    server's Retry-After.
    """
    if failures == 0:
        return max(0.0, settings.poll_seconds - elapsed)
    backoff = min(settings.poll_seconds * 2**failures, settings.max_backoff_seconds)
    return max(backoff, retry_after or 0.0)


def static_due(last_success: datetime | None, now: datetime) -> bool:
    """True if the timetable hasn't been loaded since the most recent 04:00 KL time."""
    if last_success is None:
        return True
    kl_now = now.astimezone(KL)
    boundary = kl_now.replace(hour=STATIC_REFRESH_HOUR, minute=0, second=0, microsecond=0)
    if kl_now < boundary:
        boundary -= timedelta(days=1)
    return last_success < boundary


def last_static_success(engine: Engine) -> datetime | None:
    with engine.connect() as conn:
        return conn.execute(
            select(func.max(IngestionRun.finished_at)).where(
                IngestionRun.kind == "static", IngestionRun.status == "success"
            )
        ).scalar_one()


def run_forever(
    engine: Engine,
    fetch_realtime: Callable[[], bytes],
    fetch_static: Callable[[], bytes],
    settings: Settings,
    stop: Event,
    wait: Callable[[float], object] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    wait = wait or stop.wait  # stop.wait returns early on SIGTERM
    failures = 0
    last_feed_ts: datetime | None = None
    feed_ts_loaded = False
    last_static_attempt = last_retention = -float("inf")
    stale_after = timedelta(seconds=settings.stale_after_seconds)

    while not stop.is_set():
        tick_start = time.monotonic()
        retry_after = None

        if tick_start - last_static_attempt >= STATIC_RETRY_SECONDS:
            try:
                if static_due(last_static_success(engine), now()):
                    last_static_attempt = tick_start
                    rows = import_static(engine, fetch_static)
                    log.info("timetable refreshed", extra={"rows": rows})
            except Exception:
                log.exception("timetable refresh failed")

        try:
            if not feed_ts_loaded:
                last_feed_ts = last_feed_timestamp(engine)
                feed_ts_loaded = True
            last_feed_ts = ingest_once(engine, fetch_realtime, last_feed_ts, stale_after, now())
            failures = 0
        except Exception as exc:
            failures += 1
            retry_after = exc.retry_after if isinstance(exc, FetchError) else None
            log.error(
                "realtime poll failed",
                extra={"error": repr(exc), "consecutive_failures": failures},
            )

        if tick_start - last_retention >= RETENTION_EVERY_SECONDS:
            try:
                delete_old_positions(engine, settings.retention_days, now())
                last_retention = tick_start
            except Exception:
                log.exception("retention cleanup failed")

        delay = next_delay(failures, settings, time.monotonic() - tick_start, retry_after)
        if failures:
            log.info("backing off", extra={"delay_s": delay, "consecutive_failures": failures})
        wait(delay)
    log.info("worker stopped")


def main() -> None:
    setup_logging()
    settings = Settings.from_env()
    stop = Event()
    # Docker sends SIGTERM on `docker compose stop`; finish the current tick, then exit.
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("worker starting", extra={"settings": settings.__dict__})
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        run_forever(make_engine(), partial(fetch_feed, client), download_static, settings, stop)


if __name__ == "__main__":
    main()
