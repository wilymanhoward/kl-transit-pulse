"""HTTP API. Phase 2 has only /health; Phase 4 adds the reliability endpoints.

Run: uvicorn app.api:app
"""

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, Response
from pydantic import BaseModel
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db import make_engine
from app.models import IngestionRun, VehiclePosition

app = FastAPI(
    title="KL Transit Pulse",
    description="Reliability estimates for Rapid KL buses from data.gov.my GTFS (CC BY 4.0).",
)


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
