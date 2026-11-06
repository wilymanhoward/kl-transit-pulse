"""A small, fully known day of bus data on the mini fixture timetable.

Route R1 (trip weekday_R1_R101_0) goes S1 -> S2, scheduled 10.5 min, every 30 min from
06:00. Service day: Friday 2026-11-06. 10:00 KL = 02:00 UTC.
  A: leaves 10:00, takes 12 min (on time)
  B: leaves 10:30, takes 25 min (late, long gap after A)
  C: trip ID not in the timetable (same shape R101), first seen 20 min after leaving
Polls every 30 s from 09:59 to 12:01, so hours 10 and 11 are complete.
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Engine, insert

from app.gtfs_static import import_static
from app.headways import KL
from app.models import IngestionRun, VehiclePosition

DAY = date(2026, 11, 6)
S1, S2 = (3.10, 101.60), (3.11, 101.61)  # from tests/fixtures/gtfs_mini/stops.txt


def kl(hour: int, minute: int = 0, second: int = 0) -> datetime:
    """A Kuala Lumpur wall-clock time on 2026-11-06, as an aware UTC datetime."""
    return datetime(2026, 11, 6, hour, minute, second, tzinfo=KL).astimezone(UTC)


def drive(
    vehicle: str, trip: str, start_secs: int, first_fix: datetime, minutes: float
) -> list[dict]:
    """Fixes every 30 s along a straight line from just before S1 to just past S2."""
    steps = int(minutes * 2)
    rows = []
    for i in range(steps + 1):
        f = -0.02 + 1.04 * i / steps  # start 2% before S1, end 2% past S2
        rows.append(
            {
                "vehicle_id": vehicle,
                "trip_id": trip,
                "route_id": "R1",
                "trip_start_date": DAY,
                "trip_start_secs": start_secs,
                "observed_at": first_fix + timedelta(seconds=30 * i),
                "latitude": S1[0] + f * (S2[0] - S1[0]),
                "longitude": S1[1] + f * (S2[1] - S1[1]),
            }
        )
    return rows


def load_scenario(engine: Engine, mini_zip: bytes) -> Engine:
    import_static(engine, lambda: mini_zip)
    with engine.begin() as conn:
        # Polls every 30 s from 09:59 to 12:01: hours 10 and 11 are complete.
        polls = [kl(9, 59) + timedelta(seconds=30 * i) for i in range(245)]
        run_ids = (
            conn.execute(
                insert(IngestionRun).returning(IngestionRun.id),
                [{"kind": "realtime", "status": "success", "started_at": t} for t in polls],
            )
            .scalars()
            .all()
        )
        positions = (
            # A: leaves 10:00, takes 12 min for a 10.5 min schedule.
            drive("A", "weekday_R1_R101_0", 36000, kl(10, 0, 0), 12)
            # B: leaves 10:30, takes 25 min: late, and a long gap after A.
            + drive("B", "weekday_R1_R101_0", 37800, kl(10, 30, 0), 25)
            # C: trip ID not in the timetable (same shape R101), first seen 20 min late.
            + drive("C", "weekday_R1_R101_9", 39600, kl(11, 20, 0), 12)
        )
        conn.execute(
            insert(VehiclePosition), [{**p, "ingestion_run_id": run_ids[0]} for p in positions]
        )
    return engine
