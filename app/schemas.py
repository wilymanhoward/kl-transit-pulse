"""Response models. FastAPI uses these to validate output and to generate /docs."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PlainSerializer

from app.headways import KL

DayType = Literal["weekday", "weekend"]
# Every time in a response is shown in Kuala Lumpur time, e.g. 2026-11-06T10:10:30+08:00.
KLTime = Annotated[datetime, PlainSerializer(lambda d: d.astimezone(KL).isoformat())]

ESTIMATE_NOTE = (
    "Estimates from GPS positions (data.gov.my, CC BY 4.0), not official figures. Delay is "
    "measured against each bus's actual departure plus timetabled travel time."
)
COVERAGE_NOTE = (
    "The realtime feed shows roughly a quarter to a third of scheduled buses. Buses without "
    "GPS in the feed are invisible here; coverage_pct says how much of a route we saw."
)


class ErrorBody(BaseModel):
    status: int
    message: str
    details: list[dict] | None = None


class ErrorResponse(BaseModel):
    """Every 4xx/5xx from the API (except /health, which returns its own body) looks like this."""

    error: ErrorBody


class Route(BaseModel):
    route_id: str
    short_name: str | None
    long_name: str | None


class Coverage(BaseModel):
    scheduled_departures: int | None
    runs_seen: int | None
    coverage_pct: float | None


class DelayStats(BaseModel):
    sample_size: int = Field(description="Stop arrivals measured (one bus gives many)")
    buses: int = Field(description="Distinct bus runs behind the sample")
    on_time_pct: float | None = Field(description="Arrivals between 1 min early and 5 min late")
    median_delay_secs: float | None
    p90_delay_secs: float | None


class HeadwayStats(BaseModel):
    sample_size: int
    regular_pct: float | None = Field(description="Gaps at most 3-5 min over schedule")
    bunched_pct: float | None = Field(description="Gaps under a quarter of the scheduled gap")
    median_headway_secs: float | None
    median_scheduled_headway_secs: float | None
    p90_excess_headway_secs: float | None


class RouteHour(BaseModel):
    hour: int = Field(description="KL clock hour the buses left their first stop")
    days_observed: int
    coverage: Coverage
    delay: DelayStats
    headway: HeadwayStats | None = Field(
        description="Only for route-hours where >= 80% of scheduled runs were seen"
    )


class RouteReliability(BaseModel):
    route: Route
    day_type: DayType
    hours: list[RouteHour]
    notes: list[str] = [ESTIMATE_NOTE, COVERAGE_NOTE]


class WorstRoute(BaseModel):
    rank: int
    route: Route
    on_time_pct: float
    median_delay_secs: float
    p90_delay_secs: float
    delay_sample_size: int
    delay_buses: int
    coverage_pct: float | None


class WorstRoutes(BaseModel):
    day_type: DayType
    hour: int
    min_buses: int
    total: int = Field(description="Routes meeting the filter, before limit/offset")
    limit: int
    offset: int
    items: list[WorstRoute]
    notes: list[str] = [
        ESTIMATE_NOTE,
        COVERAGE_NOTE,
        "Ranked by on-time % (lowest first), then median delay (highest first).",
    ]


class Stop(BaseModel):
    stop_id: str
    name: str
    lat: float
    lon: float


class LiveBus(BaseModel):
    route: Route
    trip_id: str
    vehicle_id: str
    departed_at: KLTime | None = Field(description="Run start from the feed")
    last_seen_at: KLTime
    current_delay_secs: int | None = Field(
        description="Delay at the last stop it passed; null if its departure wasn't seen"
    )
    estimated_arrival: KLTime | None
    minutes_away: float | None


class ScheduledBus(BaseModel):
    route: Route
    headsign: str | None
    scheduled_arrival: KLTime = Field(
        description="From a frequency-based timetable: approximate, not a fixed time"
    )
    scheduled_headway_secs: int
    typical_delay_secs: float | None = Field(
        description="Median delay at this stop for buses leaving in that hour, last 14 days"
    )
    typical_delay_sample_size: int


class NextBuses(BaseModel):
    stop: Stop
    generated_at: KLTime
    live: list[LiveBus]
    scheduled: list[ScheduledBus]
    notes: list[str] = [
        ESTIMATE_NOTE,
        COVERAGE_NOTE,
        "Rapid KL timetables say 'every N minutes', so scheduled times are an approximate grid.",
    ]
