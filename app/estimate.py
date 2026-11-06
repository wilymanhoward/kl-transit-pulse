"""Turn stored GPS positions into stop arrivals, delays, headways, coverage and stats.

For each service day:
  1. Load every bus run's fixes and detect stop arrivals (app/headways.py).
  2. Delay per arrival, for runs whose departure we saw (main measure).
  3. Headway per arrival, compared with the scheduled headway.
  4. Coverage per route and departure hour: runs seen vs runs scheduled.
  5. Replace that day's rows in stop_arrivals and route_coverage in one transaction.
Then rebuild route_stats_hourly in SQL, judging headways only where coverage >= 80%.

Re-running is safe: a day's rows are always fully replaced, never appended.

Run: python -m app.estimate [--days N]
"""

import argparse
import logging
import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Engine, delete, func, insert, select, text

from app.db import make_engine
from app.headways import (
    KL,
    Fix,
    StopPoint,
    Window,
    assign_headways,
    delay_secs,
    departure_seen,
    departures_per_hour,
    detect_arrivals,
    is_on_time,
    is_regular,
    scheduled_headway,
)
from app.logging_setup import setup_logging
from app.models import (
    Calendar,
    Frequency,
    IngestionRun,
    RouteCoverage,
    RouteStatsHourly,
    Stop,
    StopArrival,
    StopTime,
    Trip,
    VehiclePosition,
)

log = logging.getLogger(__name__)

STATS_WINDOW_DAYS = 14
MAX_GAP_S = 180.0  # same threshold as detect_arrivals: longer gaps can hide a bus
SERVICE_DAY_SPAN = timedelta(hours=28)  # GPS for day D can run to ~25:15 on the clock
MIN_HEADWAY_COVERAGE = 0.8  # judge headways only if we saw >= 80% of scheduled runs
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_trip_id(trip_id: str) -> tuple[str, str] | None:
    """'weekday_U7830_U783002_2' -> ('weekday', 'U783002'). Verified on all 2,121 static
    trips; live IDs missing from the timetable follow the same pattern."""
    parts = trip_id.split("_")
    return (parts[0], parts[2]) if len(parts) == 4 else None


def day_start(service_date: date) -> datetime:
    return datetime.combine(service_date, datetime.min.time(), KL)


# ---------- loading ----------


class Timetable:
    """The parts of the static timetable the estimator and API need, loaded in one go."""

    def __init__(self, conn) -> None:
        self.stops_by_trip: dict[str, list[StopPoint]] = defaultdict(list)
        rows = conn.execute(
            select(
                StopTime.trip_id,
                StopTime.stop_sequence,
                StopTime.arrival_secs,
                Stop.stop_id,
                Stop.stop_lat,
                Stop.stop_lon,
            )  # fmt: skip
            .join(Stop, Stop.stop_id == StopTime.stop_id)
            .order_by(StopTime.trip_id, StopTime.stop_sequence)
        )
        first_secs: dict[str, int] = {}
        for trip_id, seq, arr_secs, stop_id, lat, lon in rows:
            first = first_secs.setdefault(trip_id, arr_secs)
            self.stops_by_trip[trip_id].append(StopPoint(seq, stop_id, lat, lon, arr_secs - first))

        # A few shapes have more than one stop list; the fallback uses the most common one.
        lists_by_shape: dict[str, Counter] = defaultdict(Counter)
        first_trip: dict[tuple, str] = {}
        for trip_id, shape_id in conn.execute(select(Trip.trip_id, Trip.shape_id)):
            if shape_id and trip_id in self.stops_by_trip:
                key = tuple(s.stop_id for s in self.stops_by_trip[trip_id])
                lists_by_shape[shape_id][key] += 1
                first_trip.setdefault((shape_id, key), trip_id)
        self.trip_for_shape = {
            shape: first_trip[(shape, lists.most_common(1)[0][0])]
            for shape, lists in lists_by_shape.items()
        }

        self.windows_by_shape: dict[tuple[str, str], list[Window]] = defaultdict(list)
        self.windows_by_route: dict[tuple[str, str], list[Window]] = defaultdict(list)
        self.windows_by_trip: dict[str, list[Window]] = defaultdict(list)
        for trip_id, route_id, shape_id, service_id, start, end, headway in conn.execute(
            select(Trip.trip_id, Trip.route_id, Trip.shape_id, Trip.service_id,
                   Frequency.start_secs, Frequency.end_secs, Frequency.headway_secs)
            .join(Trip, Trip.trip_id == Frequency.trip_id)
        ):  # fmt: skip
            w = Window(start, end, headway)
            self.windows_by_shape[(shape_id, service_id)].append(w)
            self.windows_by_route[(route_id, service_id)].append(w)
            self.windows_by_trip[trip_id].append(w)

        # For the API: which trip templates stop where (a loop can list a stop twice).
        self.trip_info = {
            t.trip_id: t for t in conn.execute(select(Trip.trip_id, Trip.route_id, Trip.shape_id,
                                                      Trip.service_id, Trip.trip_headsign))
        }  # fmt: skip
        self.trips_at_stop: dict[str, list[tuple[str, StopPoint]]] = defaultdict(list)
        for trip_id, stops in self.stops_by_trip.items():
            for stop in stops:
                self.trips_at_stop[stop.stop_id].append((trip_id, stop))

        self.calendar = conn.execute(select(Calendar)).mappings().all()

    def stops_for(self, trip_id: str, shape_id: str) -> list[StopPoint] | None:
        return self.stops_by_trip.get(trip_id) or self.stops_by_trip.get(
            self.trip_for_shape.get(shape_id)
        )

    def services_on(self, d: date) -> set[str]:
        """service_ids running on date d (calendar.txt; there are no holiday exceptions)."""
        return {
            c["service_id"]
            for c in self.calendar
            if c[DAYS[d.weekday()]] and c["start_date"] <= d <= c["end_date"]
        }


def load_runs(conn, service_date: date) -> dict[tuple, list]:
    """Fixes for every bus run that started on `service_date`, grouped and time-sorted."""
    start = day_start(service_date)
    rows = conn.execute(
        select(
            VehiclePosition.vehicle_id,
            VehiclePosition.trip_id,
            VehiclePosition.route_id,
            VehiclePosition.trip_start_secs,
            VehiclePosition.observed_at,
            VehiclePosition.latitude,
            VehiclePosition.longitude,
        )  # fmt: skip
        .where(
            VehiclePosition.observed_at >= start,  # range first: uses the time index
            VehiclePosition.observed_at < start + SERVICE_DAY_SPAN,
            VehiclePosition.trip_start_date == service_date,
            VehiclePosition.trip_id.is_not(None),
        )
        .order_by(VehiclePosition.observed_at)
    )
    runs: dict[tuple, list] = defaultdict(list)
    for vehicle_id, trip_id, route_id, start_secs, t, lat, lon in rows:
        route = route_id or trip_id.split("_")[1]
        runs[(vehicle_id, trip_id, route, start_secs)].append(Fix(t, lat, lon))
    return runs


def load_outages(conn, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Periods with no realtime poll for longer than MAX_GAP_S, including before the first
    and after the last poll in the period (the worker wasn't running yet, or still is)."""
    times = list(
        conn.execute(
            select(IngestionRun.started_at)
            .where(
                IngestionRun.kind == "realtime",
                IngestionRun.status.in_(["success", "skipped"]),
                IngestionRun.started_at.between(start, end),
            )
            .order_by(IngestionRun.started_at)
        ).scalars()
    )
    edges = [start, *times, end]
    return [
        (a, b)
        for a, b in zip(edges, edges[1:], strict=False)
        if (b - a).total_seconds() > MAX_GAP_S
    ]


# ---------- estimation ----------


def arrival_rows(service_date: date, runs: dict, tt: Timetable) -> tuple[list[dict], Counter]:
    rows, skipped = [], Counter()
    for (vehicle_id, trip_id, route_id, start_secs), fixes in runs.items():
        parsed = parse_trip_id(trip_id)
        stops = tt.stops_for(trip_id, parsed[1]) if parsed else None
        if not stops:
            skipped["no stop list" if parsed else "unparseable trip_id"] += 1
            continue
        service_id, shape_id = parsed
        scheduled = scheduled_headway(
            tt.windows_by_shape.get((shape_id, service_id), []), start_secs
        )
        seen_leaving = departure_seen(fixes[0].t, service_date, start_secs)
        offsets = {s.sequence: s.offset_secs for s in stops}
        for a in detect_arrivals(fixes, stops, max_gap_s=MAX_GAP_S):
            offset = offsets[a.sequence]
            # The first stop (offset 0) is the departure itself: on time by definition.
            delay = (
                delay_secs(a.t, service_date, start_secs, offset)
                if seen_leaving and offset
                else None
            )
            rows.append(
                {
                    "service_date": service_date,
                    "route_id": route_id,
                    "shape_id": shape_id,
                    "trip_id": trip_id,
                    "trip_start_secs": start_secs,
                    "vehicle_id": vehicle_id,
                    "stop_id": a.stop_id,
                    "stop_sequence": a.sequence,
                    "arrived_at": a.t,
                    "distance_m": a.distance_m,
                    "scheduled_offset_secs": offset,
                    "delay_secs": delay,
                    "on_time": is_on_time(delay) if delay is not None else None,
                    "scheduled_headway_secs": scheduled,
                }
            )
    return rows, skipped


def coverage_rows(
    service_date: date, runs: dict, tt: Timetable, outages: list[tuple[datetime, datetime]]
) -> list[dict]:
    scheduled: dict[tuple[str, int], int] = defaultdict(int)
    services = tt.services_on(service_date)
    for (route_id, service_id), windows in tt.windows_by_route.items():
        if service_id in services:
            for hour, n in departures_per_hour(windows).items():
                scheduled[(route_id, hour)] += n
    seen: dict[tuple[str, int], int] = defaultdict(int)
    for _, _, route_id, start_secs in runs:
        if start_secs is not None:
            seen[(route_id, start_secs // 3600)] += 1

    start = day_start(service_date)

    def complete(hour: int) -> bool:
        lo, hi = start + timedelta(hours=hour), start + timedelta(hours=hour + 1)
        return not any(o_start < hi and o_end > lo for o_start, o_end in outages)

    return [
        {
            "route_id": route_id,
            "service_date": service_date,
            "hour": hour,
            "scheduled_departures": scheduled.get((route_id, hour), 0),
            "runs_seen": seen.get((route_id, hour), 0),
            "ingestion_complete": complete(hour),
        }
        for route_id, hour in scheduled.keys() | seen.keys()
        if hour <= 30
    ]


def estimate_day(engine: Engine, service_date: date) -> dict:
    with engine.connect() as conn:
        tt = Timetable(conn)
        runs = load_runs(conn, service_date)
        start = day_start(service_date)
        outages = load_outages(conn, start, start + SERVICE_DAY_SPAN)

    rows, skipped = arrival_rows(service_date, runs, tt)
    assign_headways(rows, outages)
    for r in rows:
        h, s = r["headway_secs"], r["scheduled_headway_secs"]
        r["regular"] = is_regular(h, s) if h is not None and s is not None else None
    coverage = coverage_rows(service_date, runs, tt, outages)

    with engine.begin() as conn:  # the whole day is swapped atomically
        conn.execute(delete(StopArrival).where(StopArrival.service_date == service_date))
        conn.execute(delete(RouteCoverage).where(RouteCoverage.service_date == service_date))
        if rows:
            conn.execute(insert(StopArrival), rows)
        if coverage:
            conn.execute(insert(RouteCoverage), coverage)

    summary = {
        "service_date": service_date,
        "runs": len(runs),
        "arrivals": len(rows),
        "with_delay": sum(r["delay_secs"] is not None for r in rows),
        "with_headway": sum(r["regular"] is not None for r in rows),
        "outages": len(outages),
        "skipped_runs": dict(skipped),
    }
    log.info("service day estimated", extra=summary)
    return summary


# Aggregated in Postgres. "Is this on time / regular?" was decided per row in Python
# (app.headways), so those rules live in one place. hour = KL clock hour of the bus's
# departure from its first stop, for every measure, so the columns line up.
STATS_SQL = text("""
WITH arr AS (
    SELECT a.*, a.trip_start_secs / 3600 AS dep_hour
    FROM stop_arrivals a
    WHERE a.service_date >= :since AND a.trip_start_secs IS NOT NULL
),
cov AS (
    SELECT route_id,
           CASE WHEN extract(isodow FROM service_date) >= 6 THEN 'weekend' ELSE 'weekday' END
               AS day_type,
           hour % 24 AS hour,
           count(DISTINCT service_date) AS days,
           sum(scheduled_departures) AS scheduled,
           sum(runs_seen) AS seen
    FROM route_coverage
    WHERE ingestion_complete AND service_date >= :since
    GROUP BY 1, 2, 3
),
delay AS (
    SELECT route_id,
           CASE WHEN extract(isodow FROM service_date) >= 6 THEN 'weekend' ELSE 'weekday' END
               AS day_type,
           dep_hour % 24 AS hour,
           count(DISTINCT service_date) AS days,
           count(*) AS n,
           count(DISTINCT (vehicle_id, trip_id, service_date, trip_start_secs)) AS buses,
           100.0 * avg(on_time::int) AS on_time_pct,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY delay_secs) AS median_delay,
           percentile_cont(0.9) WITHIN GROUP (ORDER BY delay_secs) AS p90_delay
    FROM arr
    WHERE delay_secs IS NOT NULL
    GROUP BY 1, 2, 3
),
headway AS (
    -- Only route-hours where we saw >= :min_cov of the scheduled runs, with no outage.
    SELECT a.route_id,
           CASE WHEN extract(isodow FROM a.service_date) >= 6 THEN 'weekend' ELSE 'weekday' END
               AS day_type,
           a.dep_hour % 24 AS hour,
           count(*) AS n,
           100.0 * avg(a.regular::int) AS regular_pct,
           100.0 * avg((a.headway_secs < a.scheduled_headway_secs / 4.0)::int) AS bunched_pct,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY a.headway_secs) AS median_headway,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY a.scheduled_headway_secs) AS median_sched,
           percentile_cont(0.9) WITHIN GROUP (ORDER BY a.headway_secs - a.scheduled_headway_secs)
               AS p90_excess
    FROM arr a
    JOIN route_coverage c
      ON (c.route_id, c.service_date, c.hour) = (a.route_id, a.service_date, a.dep_hour)
    WHERE a.regular IS NOT NULL
      AND c.ingestion_complete
      AND c.scheduled_departures > 0
      AND c.runs_seen >= :min_cov * c.scheduled_departures
    GROUP BY 1, 2, 3
),
keys AS (
    SELECT route_id, day_type, hour FROM cov
    UNION SELECT route_id, day_type, hour FROM delay
)
INSERT INTO route_stats_hourly
SELECT k.route_id, k.day_type, k.hour,
       greatest(coalesce(cov.days, 0), coalesce(delay.days, 0)),
       cov.scheduled, cov.seen,
       CASE WHEN cov.scheduled > 0 THEN 100.0 * cov.seen / cov.scheduled END,
       coalesce(delay.n, 0), coalesce(delay.buses, 0),
       delay.on_time_pct, delay.median_delay, delay.p90_delay,
       coalesce(headway.n, 0), headway.regular_pct, headway.bunched_pct,
       headway.median_headway, headway.median_sched, headway.p90_excess,
       now()
FROM keys k
LEFT JOIN cov USING (route_id, day_type, hour)
LEFT JOIN delay USING (route_id, day_type, hour)
LEFT JOIN headway USING (route_id, day_type, hour)
""")


def refresh_route_stats(engine: Engine, today: date) -> int:
    with engine.begin() as conn:
        conn.execute(delete(RouteStatsHourly))
        conn.execute(
            STATS_SQL,
            {"since": today - timedelta(days=STATS_WINDOW_DAYS), "min_cov": MIN_HEADWAY_COVERAGE},
        )
        return conn.execute(select(func.count()).select_from(RouteStatsHourly)).scalar_one()


def run_estimation(engine: Engine, days: int = 2, now: datetime | None = None) -> list[dict]:
    """Re-estimate the last `days` service days (KL time), then rebuild the stats."""
    today = (now or datetime.now(UTC)).astimezone(KL).date()
    summaries = [estimate_day(engine, today - timedelta(days=d)) for d in range(days - 1, -1, -1)]
    groups = refresh_route_stats(engine, today)
    log.info("route stats refreshed", extra={"groups": groups})
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=2, help="service days to (re)estimate")
    args = parser.parse_args()
    setup_logging()
    started = time.monotonic()
    run_estimation(make_engine(), args.days)
    log.info("estimation done", extra={"seconds": round(time.monotonic() - started, 1)})


if __name__ == "__main__":
    main()
