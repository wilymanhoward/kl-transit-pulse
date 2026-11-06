"""Fetch, decode and store GTFS Realtime vehicle positions.

decode() is pure (bytes in, rows out). ingest_once() does one full fetch -> store cycle
and records it in ingestion_runs. The polling loop and backoff live in app/worker.py.
"""

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.gtfs_static import parse_gtfs_date, parse_gtfs_time
from app.models import IngestionRun, VehiclePosition
from app.runs import fail_run, finish_run, start_run

log = logging.getLogger(__name__)

# Trailing slash matters: without it the API answers 301, costing a second request.
REALTIME_URL = (
    os.getenv("GTFS_API_BASE", "https://api.data.gov.my")
    + "/gtfs-realtime/vehicle-position/prasarana/"
)
CATEGORY = os.getenv("GTFS_CATEGORY", "rapid-bus-kl")


class FetchError(Exception):
    """A failed fetch worth retrying later. retry_after comes from the server, if sent."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class Snapshot:
    feed_timestamp: datetime
    rows: list[dict]
    dropped: int  # entities we refused to store (no id, no/invalid position)


# ---------- fetch ----------


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:  # HTTP-date form
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


def fetch_feed(client: httpx.Client, url: str = REALTIME_URL, category: str = CATEGORY) -> bytes:
    """Exactly one HTTP request. Raises FetchError on any network or HTTP failure."""
    try:
        resp = client.get(url, params={"category": category})
    except httpx.TransportError as exc:  # timeouts, DNS, refused connections...
        raise FetchError(f"network error: {exc!r}") from exc
    if resp.status_code >= 400:
        raise FetchError(f"HTTP {resp.status_code}", retry_after=_retry_after(resp))
    return resp.content


# ---------- decode (pure) ----------


def _valid_position(lat: float, lon: float) -> bool:
    # (0, 0) is a classic "no GPS fix" placeholder, not a bus in the Atlantic.
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def _try(parse: Callable[[str], object], value: str) -> object | None:
    try:
        return parse(value) if value else None
    except ValueError:
        return None


def decode(data: bytes) -> Snapshot:
    """Protobuf bytes -> rows for vehicle_positions. Raises ValueError if not a valid feed."""
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(data)
    except DecodeError as exc:
        raise ValueError(f"not a GTFS-realtime feed: {exc}") from exc
    if not feed.header.timestamp:
        raise ValueError("feed header has no timestamp")
    feed_ts = datetime.fromtimestamp(feed.header.timestamp, UTC)

    rows, dropped = [], 0
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue  # alerts / trip updates: not in this feed today
        v = entity.vehicle
        vehicle_id = v.vehicle.id or v.vehicle.license_plate
        pos = v.position
        if not vehicle_id or not v.HasField("position"):
            dropped += 1
            continue
        if not _valid_position(pos.latitude, pos.longitude):
            dropped += 1
            continue
        trip = v.trip
        rows.append(
            {
                "vehicle_id": vehicle_id,
                "trip_id": trip.trip_id or None,
                "route_id": trip.route_id or None,
                "trip_start_date": _try(parse_gtfs_date, trip.start_date),
                "trip_start_secs": _try(parse_gtfs_time, trip.start_time),
                # A vehicle's own GPS time is what we analyse; fall back to the header's.
                "observed_at": datetime.fromtimestamp(v.timestamp, UTC) if v.timestamp else feed_ts,
                "latitude": pos.latitude,
                "longitude": pos.longitude,
                "bearing": pos.bearing if pos.HasField("bearing") else None,
                "speed_mps": pos.speed if pos.HasField("speed") else None,
            }
        )
    return Snapshot(feed_ts, rows, dropped)


# ---------- store ----------


def last_feed_timestamp(engine: Engine) -> datetime | None:
    """Newest snapshot header we've already processed (survives restarts)."""
    with engine.connect() as conn:
        return conn.execute(
            select(func.max(IngestionRun.feed_timestamp)).where(
                IngestionRun.kind == "realtime", IngestionRun.status.in_(["success", "skipped"])
            )
        ).scalar_one()


def ingest_once(
    engine: Engine,
    fetch: Callable[[], bytes],
    last_feed_ts: datetime | None,
    stale_after: timedelta,
    now: datetime,
) -> datetime:
    """One fetch -> decode -> store cycle. Returns the snapshot's header timestamp.

    Raises on failure (after recording the run as failed) so the caller can back off.
    """
    run_id = start_run(engine, "realtime")
    try:
        snap = decode(fetch())
        if last_feed_ts is not None and snap.feed_timestamp <= last_feed_ts:
            reason = "duplicate"
        elif now - snap.feed_timestamp > stale_after:
            reason = "stale"
        else:
            reason = None
        if reason:
            finish_run(
                engine, run_id, "skipped", feed_timestamp=snap.feed_timestamp, rows_written=0
            )
            log.warning(
                "snapshot skipped",
                extra={"reason": reason, "run_id": run_id, "feed_ts": snap.feed_timestamp},
            )
            return snap.feed_timestamp

        inserted = 0
        if snap.rows:
            with engine.begin() as conn:
                # Same bus + same GPS time seen in an earlier snapshot: keep the first copy.
                stmt = (
                    pg_insert(VehiclePosition)
                    .values([{**r, "ingestion_run_id": run_id} for r in snap.rows])
                    .on_conflict_do_nothing(constraint="uq_vehicle_positions_fix")
                    .returning(VehiclePosition.id)
                )
                inserted = len(conn.execute(stmt).all())
    except Exception as exc:
        fail_run(engine, run_id, exc)
        raise
    finish_run(engine, run_id, "success", feed_timestamp=snap.feed_timestamp, rows_written=inserted)
    log.info(
        "snapshot stored",
        extra={
            "run_id": run_id,
            "vehicles": len(snap.rows),
            "inserted": inserted,
            "duplicates": len(snap.rows) - inserted,
            "dropped": snap.dropped,
            "feed_age_s": round((now - snap.feed_timestamp).total_seconds()),
        },
    )
    return snap.feed_timestamp


def delete_old_positions(engine: Engine, retention_days: int, now: datetime) -> int:
    cutoff = now - timedelta(days=retention_days)
    with engine.begin() as conn:
        deleted = conn.execute(
            delete(VehiclePosition).where(VehiclePosition.observed_at < cutoff)
        ).rowcount
    log.info("retention cleanup", extra={"deleted": deleted, "cutoff": cutoff})
    return deleted
