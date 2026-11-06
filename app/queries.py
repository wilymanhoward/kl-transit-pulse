"""Database reads behind the API. Endpoints in app/api.py stay thin: validate, call one of
these, shape the response."""

from collections.abc import Iterable, Iterator
from datetime import date, datetime, timedelta

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from app.estimate import Timetable, day_start, parse_trip_id
from app.headways import KL, Fix, Window, live_estimate
from app.models import IngestionRun, Route, RouteStatsHourly, Stop, VehiclePosition
from app.schemas import (
    Coverage,
    DelayStats,
    HeadwayStats,
    LiveBus,
    RouteHour,
    ScheduledBus,
    WorstRoute,
)
from app.schemas import Route as RouteOut

LIVE_SEEN_WITHIN = timedelta(minutes=5)  # a bus not heard from in 5 min isn't "live"
LIVE_HISTORY = timedelta(hours=4)  # GPS history used to place a live bus on its route
SCHEDULE_HORIZON = timedelta(minutes=90)
TYPICAL_DELAY_DAYS = 14


def day_type_of(d: date) -> str:
    return "weekend" if d.weekday() >= 5 else "weekday"


# ---------- routes ----------


def get_route(conn, route_id: str) -> RouteOut | None:
    row = conn.execute(
        select(Route.route_id, Route.route_short_name, Route.route_long_name).where(
            Route.route_id == route_id
        )
    ).first()
    return RouteOut(route_id=row[0], short_name=row[1], long_name=row[2]) if row else None


def _route_hour(r: RouteStatsHourly) -> RouteHour:
    return RouteHour(
        hour=r.hour,
        days_observed=r.days_observed,
        coverage=Coverage(
            scheduled_departures=r.scheduled_departures,
            runs_seen=r.runs_seen,
            coverage_pct=r.coverage_pct,
        ),
        delay=DelayStats(
            sample_size=r.delay_sample_size,
            buses=r.delay_buses,
            on_time_pct=r.on_time_pct,
            median_delay_secs=r.median_delay_secs,
            p90_delay_secs=r.p90_delay_secs,
        ),
        headway=HeadwayStats(
            sample_size=r.headway_sample_size,
            regular_pct=r.regular_pct,
            bunched_pct=r.bunched_pct,
            median_headway_secs=r.median_headway_secs,
            median_scheduled_headway_secs=r.median_scheduled_headway_secs,
            p90_excess_headway_secs=r.p90_excess_headway_secs,
        )
        if r.headway_sample_size
        else None,
    )


def route_hours(conn, route_id: str, day_type: str, hour: int | None) -> list[RouteHour]:
    q = select(RouteStatsHourly).where(
        RouteStatsHourly.route_id == route_id, RouteStatsHourly.day_type == day_type
    )
    if hour is not None:
        q = q.where(RouteStatsHourly.hour == hour)
    rows = Session(bind=conn).scalars(q.order_by(RouteStatsHourly.hour))  # ORM rows
    return [_route_hour(r) for r in rows]


def worst_routes(
    conn, day_type: str, hour: int, min_buses: int, limit: int, offset: int
) -> tuple[list[WorstRoute], int]:
    """Routes for that hour ranked worst first, ignoring thin samples (< min_buses runs)."""
    s, r = RouteStatsHourly, Route
    where = (
        s.day_type == day_type,
        s.hour == hour,
        s.delay_buses >= min_buses,
        s.on_time_pct.is_not(None),
    )
    total = conn.execute(select(func.count()).select_from(s).where(*where)).scalar_one()
    rows = (
        Session(bind=conn)
        .execute(  # ORM: rows of (stats object, short, long)
            select(s, r.route_short_name, r.route_long_name)
            .outerjoin(r, r.route_id == s.route_id)  # stats can outlive a route in the timetable
            .where(*where)
            .order_by(s.on_time_pct.asc(), s.median_delay_secs.desc(), s.route_id)
            .limit(limit)
            .offset(offset)
        )
        .all()
    )
    items = [
        WorstRoute(
            rank=offset + i + 1,
            route=RouteOut(route_id=st.route_id, short_name=short, long_name=long_),
            on_time_pct=st.on_time_pct,
            median_delay_secs=st.median_delay_secs,
            p90_delay_secs=st.p90_delay_secs,
            delay_sample_size=st.delay_sample_size,
            delay_buses=st.delay_buses,
            coverage_pct=st.coverage_pct,
        )
        for i, (st, short, long_) in enumerate(rows)
    ]
    return items, total


# ---------- stops ----------


def get_stop(conn, stop_id: str) -> Stop | None:
    return Session(bind=conn).scalars(select(Stop).where(Stop.stop_id == stop_id)).first()


def grid_times(
    windows: Iterable[Window], offset_secs: int, service_start: datetime, lo: datetime, hi: datetime
) -> Iterator[tuple[datetime, int]]:
    """(time at this stop, headway) for each timetabled departure that reaches the stop
    between lo and hi. Pure: the frequency grid start, start + headway, ... before end,
    shifted by the travel time to this stop."""
    for w in windows:
        for dep in range(w.start_secs, w.end_secs, w.headway_secs):
            t = service_start + timedelta(seconds=dep + offset_secs)
            if lo <= t <= hi:
                yield t, w.headway_secs


TYPICAL_DELAY_SQL = text("""
SELECT shape_id, stop_sequence, (trip_start_secs / 3600) % 24 AS hour,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY delay_secs) AS median, count(*) AS n
FROM stop_arrivals
WHERE stop_id = :stop_id AND service_date >= :since AND delay_secs IS NOT NULL
  AND (extract(isodow FROM service_date) >= 6) = :weekend
GROUP BY 1, 2, 3
""")


def scheduled_at_stop(
    conn, tt: Timetable, routes: dict, stop_id: str, now: datetime, limit: int
) -> list[ScheduledBus]:
    today = now.astimezone(KL).date()
    typical = {
        (r.shape_id, r.stop_sequence, r.hour): (r.median, r.n)
        for r in conn.execute(
            TYPICAL_DELAY_SQL,
            {
                "stop_id": stop_id,
                "since": today - timedelta(days=TYPICAL_DELAY_DAYS),
                "weekend": day_type_of(today) == "weekend",
            },
        )
    }
    out = []
    # Yesterday's service day too: its after-midnight trips (24:xx, 25:xx) run today.
    for service_date in (today - timedelta(days=1), today):
        services = tt.services_on(service_date)
        start = day_start(service_date)
        for trip_id, stop in tt.trips_at_stop.get(stop_id, []):
            info = tt.trip_info.get(trip_id)
            if info is None or info.service_id not in services:
                continue
            for t, headway in grid_times(
                tt.windows_by_trip.get(trip_id, []),
                stop.offset_secs,
                start,
                now,
                now + SCHEDULE_HORIZON,
            ):
                dep_hour = int((t - start).total_seconds() - stop.offset_secs) // 3600 % 24
                median, n = typical.get((info.shape_id, stop.sequence, dep_hour), (None, 0))
                out.append(
                    ScheduledBus(
                        route=routes.get(info.route_id)
                        or RouteOut(route_id=info.route_id, short_name=None, long_name=None),
                        headsign=info.trip_headsign,
                        scheduled_arrival=t,
                        scheduled_headway_secs=headway,
                        typical_delay_secs=median,
                        typical_delay_sample_size=n,
                    )
                )
    out.sort(key=lambda b: (b.scheduled_arrival, b.route.route_id))
    return out[:limit]


def live_at_stop(
    conn, tt: Timetable, routes: dict, stop_id: str, now: datetime, limit: int
) -> list[LiveBus]:
    """Buses heard from in the last 5 min whose run still has `stop_id` ahead of it."""
    v = VehiclePosition
    latest = conn.execute(
        select(
            v.vehicle_id, v.trip_id, v.route_id, v.trip_start_date, v.trip_start_secs, v.observed_at
        )
        .where(v.observed_at > now - LIVE_SEEN_WITHIN, v.observed_at <= now, v.trip_id.is_not(None))
        .order_by(v.vehicle_id, v.observed_at.desc())
        .distinct(v.vehicle_id)  # Postgres DISTINCT ON: newest fix per vehicle
    ).all()
    candidates = {}
    for row in latest:
        parsed = parse_trip_id(row.trip_id)
        stops = tt.stops_for(row.trip_id, parsed[1]) if parsed else None
        if stops and any(s.stop_id == stop_id for s in stops):
            candidates[(row.vehicle_id, row.trip_id, row.trip_start_date, row.trip_start_secs)] = (
                row,
                stops,
            )
    if not candidates:
        return []

    fixes: dict[tuple, list[Fix]] = {k: [] for k in candidates}
    for vehicle_id, trip_id, start_date, start_secs, t, lat, lon in conn.execute(
        select(v.vehicle_id, v.trip_id, v.trip_start_date, v.trip_start_secs, v.observed_at,
               v.latitude, v.longitude)
        .where(v.vehicle_id.in_([k[0] for k in candidates]), v.observed_at > now - LIVE_HISTORY,
               v.observed_at <= now)
        .order_by(v.observed_at)
    ):  # fmt: skip
        key = (vehicle_id, trip_id, start_date, start_secs)
        if key in fixes:
            fixes[key].append(Fix(t, lat, lon))

    out = []
    for key, (row, stops) in candidates.items():
        est = live_estimate(fixes[key], stops, stop_id, row.trip_start_date, row.trip_start_secs)
        if est is None:
            continue  # already passed (its last visit to) this stop
        service_date, start_secs = row.trip_start_date, row.trip_start_secs
        eta = est.eta
        route_id = row.route_id or row.trip_id.split("_")[1]
        out.append(
            LiveBus(
                route=routes.get(route_id)
                or RouteOut(route_id=route_id, short_name=None, long_name=None),
                trip_id=row.trip_id,
                vehicle_id=row.vehicle_id,
                departed_at=day_start(service_date) + timedelta(seconds=start_secs)
                if service_date and start_secs is not None
                else None,
                last_seen_at=row.observed_at,
                current_delay_secs=est.current_delay_secs,
                estimated_arrival=eta,
                minutes_away=round(max(0.0, (eta - now).total_seconds() / 60), 1) if eta else None,
            )
        )
    out.sort(
        key=lambda b: (b.estimated_arrival is None, b.estimated_arrival or now, b.route.route_id)
    )
    return out[:limit]


def routes_by_id(conn) -> dict[str, RouteOut]:
    return {
        rid: RouteOut(route_id=rid, short_name=short, long_name=long_)
        for rid, short, long_ in conn.execute(
            select(Route.route_id, Route.route_short_name, Route.route_long_name)
        )
    }


def last_static_load(conn) -> datetime | None:
    return conn.execute(
        select(func.max(IngestionRun.finished_at)).where(
            IngestionRun.kind == "static", IngestionRun.status == "success"
        )
    ).scalar_one()


class TimetableCache:
    """Loading the timetable takes ~1-2 s, so keep one per process and reload only when
    a newer static load has finished (daily)."""

    def __init__(self) -> None:
        self.loaded_for: datetime | None = None
        self.value: tuple[Timetable, dict] | None = None

    def get(self, engine: Engine) -> tuple[Timetable, dict]:
        with engine.connect() as conn:
            stamp = last_static_load(conn)
            if self.value is None or stamp != self.loaded_for:
                self.value = (Timetable(conn), routes_by_id(conn))
                self.loaded_for = stamp
        return self.value
