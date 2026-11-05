"""Download, parse and load the static GTFS timetable.

Parsing is pure (ZIP bytes in, rows out) so it's testable without a database.
Loading replaces every static table inside ONE transaction: readers see either the
old timetable or the new one, never a half-loaded mix, and a failed load leaves the
old data untouched.

Run: python -m app.gtfs_static
"""

import csv
import io
import logging
import os
import time
import zipfile
from collections.abc import Callable
from datetime import date, datetime

import httpx
from sqlalchemy import Engine, delete, func, insert, update

from app.db import make_engine
from app.models import Calendar, Frequency, IngestionRun, Route, Stop, StopTime, Trip

log = logging.getLogger(__name__)

STATIC_URL = (
    os.getenv("GTFS_API_BASE", "https://api.data.gov.my") + "/gtfs-static/prasarana/"
)  # trailing slash avoids a 301 hop
CATEGORY = os.getenv("GTFS_CATEGORY", "rapid-bus-kl")

Rows = list[dict]

# Parents before children: insert in this order, delete in reverse (foreign keys).
LOAD_ORDER = [Route, Stop, Calendar, Trip, StopTime, Frequency]
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ---------- parsing (pure) ----------


def parse_gtfs_time(value: str) -> int:
    """'25:15:00' -> 90900 seconds after midnight of the service day. Hours may exceed 23."""
    parts = value.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"bad GTFS time: {value!r}")
    h, m, s = (int(p) for p in parts)
    if not (0 <= m < 60 and 0 <= s < 60 and h >= 0):
        raise ValueError(f"bad GTFS time: {value!r}")
    return h * 3600 + m * 60 + s


def parse_gtfs_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y%m%d").date()


def _opt(value: str | None) -> str | None:
    """GTFS uses empty fields for 'not set'; store those as NULL."""
    if value is None:
        return None
    return value.strip() or None


def _read(zf: zipfile.ZipFile, name: str) -> Rows:
    if name not in zf.namelist():
        raise ValueError(f"GTFS feed is missing required file {name}")
    with zf.open(name) as f:
        # utf-8-sig strips a byte-order mark if the publisher adds one.
        return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))


def parse_feed(zip_bytes: bytes) -> dict[type, Rows]:
    """Turn a GTFS ZIP into rows ready to insert, keyed by model class."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        raw = {
            name: _read(zf, f"{name}.txt")
            for name in ("routes", "stops", "calendar", "trips", "stop_times", "frequencies")
        }
    return {
        Route: [
            {
                "route_id": r["route_id"],
                "agency_id": _opt(r.get("agency_id")),
                "route_short_name": _opt(r.get("route_short_name")),
                "route_long_name": _opt(r.get("route_long_name")),
                "route_type": int(r["route_type"]),
                "route_color": _opt(r.get("route_color")),
                "route_text_color": _opt(r.get("route_text_color")),
            }
            for r in raw["routes"]
        ],
        Stop: [
            {
                "stop_id": r["stop_id"],
                "stop_name": r["stop_name"],
                "stop_desc": _opt(r.get("stop_desc")),
                "stop_lat": float(r["stop_lat"]),
                "stop_lon": float(r["stop_lon"]),
            }
            for r in raw["stops"]
        ],
        Calendar: [
            {
                "service_id": r["service_id"],
                **{day: r[day] == "1" for day in DAYS},
                "start_date": parse_gtfs_date(r["start_date"]),
                "end_date": parse_gtfs_date(r["end_date"]),
            }
            for r in raw["calendar"]
        ],
        Trip: [
            {
                "trip_id": r["trip_id"],
                "route_id": r["route_id"],
                "service_id": r["service_id"],
                "shape_id": _opt(r.get("shape_id")),
                "trip_headsign": _opt(r.get("trip_headsign")),
                "direction_id": int(r["direction_id"]) if _opt(r.get("direction_id")) else None,
            }
            for r in raw["trips"]
        ],
        StopTime: [
            {
                "trip_id": r["trip_id"],
                "stop_sequence": int(r["stop_sequence"]),
                "stop_id": r["stop_id"],
                "arrival_secs": parse_gtfs_time(r["arrival_time"]),
                "departure_secs": parse_gtfs_time(r["departure_time"]),
                "stop_headsign": _opt(r.get("stop_headsign")),
            }
            for r in raw["stop_times"]
        ],
        Frequency: [
            {
                "trip_id": r["trip_id"],
                "start_secs": parse_gtfs_time(r["start_time"]),
                "end_secs": parse_gtfs_time(r["end_time"]),
                "headway_secs": int(r["headway_secs"]),
                # GTFS default for a missing exact_times is 0 (headway-based).
                "exact_times": r.get("exact_times", "0") == "1",
            }
            for r in raw["frequencies"]
        ],
    }


# ---------- I/O ----------


def download(url: str = STATIC_URL, category: str = CATEGORY) -> bytes:
    """One request; on HTTP 429 wait 60 s and retry once. (Daily job, 1 req/day.)"""
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        for attempt in range(2):
            resp = client.get(url, params={"category": category})
            if resp.status_code == 429 and attempt == 0:
                log.warning("static download rate limited (429), retrying in 60 s")
                time.sleep(60)
                continue
            resp.raise_for_status()
            return resp.content
    raise RuntimeError("static download still rate limited after retry")


def load_feed(engine: Engine, tables: dict[type, Rows]) -> int:
    """Replace all static tables atomically. Returns rows written."""
    with engine.begin() as conn:  # commits on success, rolls back on any exception
        for model in reversed(LOAD_ORDER):
            conn.execute(delete(model))
        for model in LOAD_ORDER:
            if tables[model]:
                conn.execute(insert(model), tables[model])
    return sum(len(rows) for rows in tables.values())


def import_static(engine: Engine, fetch: Callable[[], bytes] = download) -> int:
    """Download + parse + load, recording the attempt in ingestion_runs either way."""
    with engine.begin() as conn:
        run_id = conn.execute(
            insert(IngestionRun).values(kind="static").returning(IngestionRun.id)
        ).scalar_one()
    try:
        rows = load_feed(engine, parse_feed(fetch()))
    except Exception as exc:
        with engine.begin() as conn:
            conn.execute(
                update(IngestionRun)
                .where(IngestionRun.id == run_id)
                .values(status="failed", finished_at=func.now(), error=repr(exc)[:2000])
            )
        raise
    with engine.begin() as conn:
        conn.execute(
            update(IngestionRun)
            .where(IngestionRun.id == run_id)
            .values(status="success", finished_at=func.now(), rows_written=rows)
        )
    return rows


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = time.monotonic()
    rows = import_static(make_engine())
    log.info("static GTFS loaded: %d rows in %.1f s", rows, time.monotonic() - started)


if __name__ == "__main__":
    main()
