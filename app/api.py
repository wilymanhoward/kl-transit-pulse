"""HTTP API: health, route reliability, worst routes, and next buses at a stop.

Endpoints stay thin: validate input, read through app/queries.py, return a schema from
app/schemas.py. Every error uses one JSON shape (see ErrorResponse).

Run: uvicorn app.api:app
"""

import logging
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import queries
from app.cache import TTLCache
from app.config import Settings
from app.db import make_engine
from app.headways import KL
from app.models import IngestionRun, VehiclePosition
from app.schemas import (
    DayType,
    ErrorResponse,
    NextBuses,
    RouteReliability,
    Stop,
    WorstRoutes,
)

log = logging.getLogger(__name__)

app = FastAPI(
    title="KL Transit Pulse",
    description=(
        "Reliability estimates for Rapid KL buses, from data.gov.my GTFS feeds (CC BY 4.0). "
        "All figures are estimates from GPS and come with sample sizes."
    ),
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)

stats_cache = TTLCache(ttl_seconds=60)  # route stats change every 15 min
next_cache = TTLCache(ttl_seconds=15)  # live positions change every 30 s
timetables = queries.TimetableCache()

# Path and query parameter rules, shared so every endpoint validates the same way.
RouteId = Annotated[str, Path(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")]
StopId = Annotated[str, Path(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")]
Hour = Annotated[int | None, Query(ge=0, le=23, description="KL clock hour, 0-23")]


# ---------- errors: one shape for everything ----------


def _error(status: int, message: str, details: list | None = None) -> JSONResponse:
    body = {"error": {"status": status, "message": message, "details": details}}
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    return _error(exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {"field": ".".join(str(p) for p in e["loc"][1:]) or e["loc"][0], "problem": e["msg"]}
        for e in exc.errors()
    ]
    return _error(422, "invalid request", details)


@app.exception_handler(SQLAlchemyError)
async def database_error(_: Request, exc: SQLAlchemyError) -> JSONResponse:
    log.error("database error", extra={"error": repr(exc)[:500]})
    return _error(503, "database unavailable")  # no SQL or internals leak to clients


def utc_now() -> datetime:
    return datetime.now(UTC)


@lru_cache
def get_engine() -> Engine:
    return make_engine()


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()


class Health(BaseModel):
    status: str  # "ok" | "unhealthy"
    problems: list[str]
    last_realtime_success_at: datetime | None = None
    latest_observation_at: datetime | None = None
    positions_last_hour: int | None = None
    last_timetable_load_at: datetime | None = None


def _last_success(conn, kind: str):
    return conn.execute(
        select(func.max(IngestionRun.finished_at)).where(
            IngestionRun.kind == kind, IngestionRun.status == "success"
        )
    ).scalar_one()


@app.get("/health", response_model=Health)
def health(
    response: Response,
    engine: Annotated[Engine, Depends(get_engine)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Health:
    """503 when ingestion has stopped or the feed is frozen, so hosts can alert on it."""
    now = datetime.now(UTC)
    stale = timedelta(seconds=settings.stale_after_seconds)
    try:
        with engine.connect() as conn:
            last_rt = _last_success(conn, "realtime")
            last_static = _last_success(conn, "static")
            latest_obs = conn.execute(select(func.max(VehiclePosition.observed_at))).scalar_one()
            last_hour = conn.execute(
                select(func.count()).where(VehiclePosition.observed_at > now - timedelta(hours=1))
            ).scalar_one()
    except SQLAlchemyError:
        response.status_code = 503
        return Health(status="unhealthy", problems=["database unreachable"])

    problems = []
    if last_rt is None or now - last_rt > stale:
        problems.append(
            f"no successful realtime ingestion in the last {int(stale.total_seconds())} s"
        )
    # Runs can "succeed" while the feed is frozen; the newest GPS fix catches that.
    if latest_obs is None or now - latest_obs > stale:
        problems.append(f"newest vehicle position is older than {int(stale.total_seconds())} s")
    if last_static is None:
        problems.append("timetable has never been loaded")

    if problems:
        response.status_code = 503
    return Health(
        status="unhealthy" if problems else "ok",
        problems=problems,
        last_realtime_success_at=last_rt,
        latest_observation_at=latest_obs,
        positions_last_hour=last_hour,
        last_timetable_load_at=last_static,
    )


# ---------- reliability ----------


def _defaults(day_type: str | None, now: datetime) -> str:
    return day_type or queries.day_type_of(now.astimezone(KL).date())


@app.get("/routes/worst", response_model=WorstRoutes)
def worst_routes(
    engine: Annotated[Engine, Depends(get_engine)],
    hour: Hour = None,
    day_type: DayType | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    offset: Annotated[int, Query(ge=0, le=1000)] = 0,
    min_buses: Annotated[
        int, Query(ge=1, le=1000, description="Ignore route-hours with fewer bus runs")
    ] = 5,
) -> WorstRoutes:
    """Least reliable routes for an hour (default: the current KL hour and day type)."""
    now = utc_now()
    day_type = _defaults(day_type, now)
    hour = now.astimezone(KL).hour if hour is None else hour

    def compute() -> WorstRoutes:
        with engine.connect() as conn:
            items, total = queries.worst_routes(conn, day_type, hour, min_buses, limit, offset)
        return WorstRoutes(
            day_type=day_type, hour=hour, min_buses=min_buses, total=total,
            limit=limit, offset=offset, items=items,
        )  # fmt: skip

    return stats_cache.get_or_set(("worst", day_type, hour, limit, offset, min_buses), compute)


@app.get("/routes/{route_id}/reliability", response_model=RouteReliability)
def route_reliability(
    route_id: RouteId,
    engine: Annotated[Engine, Depends(get_engine)],
    hour: Hour = None,
    day_type: DayType | None = None,
) -> RouteReliability:
    """Per-hour reliability for one route (all hours, or just `hour`), last 14 days."""
    day_type = _defaults(day_type, utc_now())

    def compute() -> RouteReliability:
        with engine.connect() as conn:
            route = queries.get_route(conn, route_id)
            if route is None:
                raise HTTPException(404, f"route {route_id!r} not found")
            hours = queries.route_hours(conn, route_id, day_type, hour)
        return RouteReliability(route=route, day_type=day_type, hours=hours)

    return stats_cache.get_or_set(("reliability", route_id, day_type, hour), compute)


@app.get("/stops/{stop_id}/next", response_model=NextBuses)
def next_buses(
    stop_id: StopId,
    engine: Annotated[Engine, Depends(get_engine)],
    limit: Annotated[int, Query(ge=1, le=50, description="Max items per list")] = 10,
) -> NextBuses:
    """Buses heading to this stop now (live GPS) and the timetable for the next 90 min,
    each with an estimated or typical delay where we have data."""

    def compute() -> NextBuses:
        now = utc_now()
        tt, routes = timetables.get(engine)
        with engine.connect() as conn:
            stop = queries.get_stop(conn, stop_id)
            if stop is None:
                raise HTTPException(404, f"stop {stop_id!r} not found")
            live = queries.live_at_stop(conn, tt, routes, stop_id, now, limit)
            scheduled = queries.scheduled_at_stop(conn, tt, routes, stop_id, now, limit)
        return NextBuses(
            stop=Stop(
                stop_id=stop.stop_id, name=stop.stop_name, lat=stop.stop_lat, lon=stop.stop_lon
            ),
            generated_at=now,
            live=live,
            scheduled=scheduled,
        )

    return next_cache.get_or_set(("next", stop_id, limit), compute)
