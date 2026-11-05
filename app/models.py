"""Database tables. Column names follow the GTFS spec so they map 1:1 to the source files.

GTFS times can exceed 24:00:00 (a trip that starts at 23:50 runs to 25:10 on the same
"service day"). Postgres TIME can't hold that, so times are stored as integer seconds
after midnight of the service day (`*_secs`), e.g. 25:15:00 -> 90900.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------- static GTFS (fully replaced on each daily load) ----------


class Route(Base):
    __tablename__ = "routes"
    route_id: Mapped[str] = mapped_column(String, primary_key=True)
    agency_id: Mapped[str | None]
    route_short_name: Mapped[str | None]
    route_long_name: Mapped[str | None]
    route_type: Mapped[int] = mapped_column(SmallInteger)
    route_color: Mapped[str | None]
    route_text_color: Mapped[str | None]


class Stop(Base):
    __tablename__ = "stops"
    stop_id: Mapped[str] = mapped_column(String, primary_key=True)
    stop_name: Mapped[str]
    stop_desc: Mapped[str | None]
    stop_lat: Mapped[float] = mapped_column(Float)
    stop_lon: Mapped[float] = mapped_column(Float)


class Calendar(Base):
    __tablename__ = "calendar"
    service_id: Mapped[str] = mapped_column(String, primary_key=True)
    monday: Mapped[bool]
    tuesday: Mapped[bool]
    wednesday: Mapped[bool]
    thursday: Mapped[bool]
    friday: Mapped[bool]
    saturday: Mapped[bool]
    sunday: Mapped[bool]
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)


class Trip(Base):
    __tablename__ = "trips"
    trip_id: Mapped[str] = mapped_column(String, primary_key=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.route_id"))
    service_id: Mapped[str] = mapped_column(ForeignKey("calendar.service_id"))
    shape_id: Mapped[str | None]
    trip_headsign: Mapped[str | None]
    direction_id: Mapped[int | None] = mapped_column(SmallInteger)
    # "Which trips run on route X?" (reliability/worst-route queries start from a route).
    __table_args__ = (Index("ix_trips_route_id", "route_id"),)


class StopTime(Base):
    __tablename__ = "stop_times"
    # PK (trip_id, stop_sequence) also serves "stops of trip X in order".
    trip_id: Mapped[str] = mapped_column(ForeignKey("trips.trip_id"), primary_key=True)
    stop_sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    stop_id: Mapped[str] = mapped_column(ForeignKey("stops.stop_id"))
    arrival_secs: Mapped[int] = mapped_column(Integer)
    departure_secs: Mapped[int] = mapped_column(Integer)
    stop_headsign: Mapped[str | None]
    __table_args__ = (
        # "Which trips serve stop X?" (/stops/{id}/next); the PK can't answer it.
        Index("ix_stop_times_stop_id", "stop_id"),
        CheckConstraint("departure_secs >= arrival_secs", name="ck_stop_times_dep_after_arr"),
    )


class Frequency(Base):
    """Headway windows: 'trip template X runs every headway_secs between start and end'."""

    __tablename__ = "frequencies"
    # PK (trip_id, start_secs) serves "headway for trip X at time T".
    trip_id: Mapped[str] = mapped_column(ForeignKey("trips.trip_id"), primary_key=True)
    start_secs: Mapped[int] = mapped_column(Integer, primary_key=True)
    end_secs: Mapped[int] = mapped_column(Integer)
    headway_secs: Mapped[int] = mapped_column(Integer)
    exact_times: Mapped[bool]
    __table_args__ = (
        CheckConstraint("headway_secs > 0", name="ck_frequencies_headway_positive"),
        CheckConstraint("end_secs > start_secs", name="ck_frequencies_window"),
    )


# ---------- operational data ----------


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, server_default="running")
    rows_written: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("kind IN ('realtime', 'static')", name="ck_ingestion_runs_kind"),
        CheckConstraint(
            "status IN ('running', 'success', 'failed')", name="ck_ingestion_runs_status"
        ),
        # /health asks "latest successful realtime run"; partial index keeps it tiny.
        Index(
            "ix_ingestion_runs_last_success",
            "kind",
            "finished_at",
            postgresql_where=text("status = 'success'"),
        ),
    )


class VehiclePosition(Base):
    """One GPS fix. No FK to trips/routes: 15% of live trip_ids aren't in the static
    feed, and static tables are replaced daily."""

    __tablename__ = "vehicle_positions"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ingestion_run_id: Mapped[int] = mapped_column(ForeignKey("ingestion_runs.id"))
    vehicle_id: Mapped[str]
    trip_id: Mapped[str | None]
    route_id: Mapped[str | None]
    # trip_id is a reused template; (trip_id, start_date, start_secs) identifies one run.
    trip_start_date: Mapped[date | None] = mapped_column(Date)
    trip_start_secs: Mapped[int | None]
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    bearing: Mapped[float | None] = mapped_column(Float)
    speed_mps: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (
        # Same bus + same GPS timestamp = same fix seen in two snapshots; dedupe on insert.
        # Also serves "trajectory of vehicle X ordered by time".
        UniqueConstraint("vehicle_id", "observed_at", name="uq_vehicle_positions_fix"),
        # Retention delete (observed_at < cutoff) and /health "rows in last hour".
        Index("ix_vehicle_positions_observed_at", "observed_at"),
    )
