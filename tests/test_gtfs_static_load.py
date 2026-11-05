"""Loader tests against a real Postgres (skipped if TEST_DATABASE_URL is unset).

Each test rebuilds the schema through Alembic, so the migration itself is tested too.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select

from app.gtfs_static import import_static
from app.models import IngestionRun, StopTime, Trip
from tests.conftest import FIXTURE_DIR, make_zip

TEST_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    cfg = Config(str(ROOT / "alembic.ini"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    eng = create_engine(TEST_URL)
    yield eng
    eng.dispose()


def count(engine: Engine, model: type) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(model)).scalar_one()


def test_loading_twice_does_not_duplicate(engine: Engine, mini_zip: bytes) -> None:
    assert import_static(engine, lambda: mini_zip) == 19
    assert import_static(engine, lambda: mini_zip) == 19
    assert count(engine, Trip) == 3
    assert count(engine, StopTime) == 6


def test_reload_replaces_old_rows(engine: Engine, mini_zip: bytes) -> None:
    import_static(engine, lambda: mini_zip)
    trips = (FIXTURE_DIR / "trips.txt").read_text().splitlines()
    stop_times = (FIXTURE_DIR / "stop_times.txt").read_text().splitlines()
    freqs = (FIXTURE_DIR / "frequencies.txt").read_text().splitlines()
    drop = "weekend_R1_R101_0"
    smaller = make_zip(
        {
            name: "\n".join(line for line in lines if drop not in line) + "\n"
            for name, lines in [
                ("trips.txt", trips),
                ("stop_times.txt", stop_times),
                ("frequencies.txt", freqs),
            ]
        }
    )
    import_static(engine, lambda: smaller)
    with engine.connect() as conn:
        ids = set(conn.execute(select(Trip.trip_id)).scalars())
    assert ids == {"weekday_R1_R101_0", "weekday_R2_R202_0"}


def test_past_midnight_times_stored_as_seconds(engine: Engine, mini_zip: bytes) -> None:
    import_static(engine, lambda: mini_zip)
    with engine.connect() as conn:
        latest = conn.execute(select(func.max(StopTime.arrival_secs))).scalar_one()
    assert latest == 87000  # 24:10:00


def test_failed_load_keeps_old_data_and_records_failure(engine: Engine, mini_zip: bytes) -> None:
    import_static(engine, lambda: mini_zip)
    stop_times = (FIXTURE_DIR / "stop_times.txt").read_text()
    broken = make_zip(
        {"stop_times.txt": stop_times + "weekday_R1_R101_0,07:00:00,07:00:00,NOPE,3,\n"}
    )

    with pytest.raises(Exception, match="foreign key"):
        import_static(engine, lambda: broken)

    assert count(engine, StopTime) == 6  # rolled back, previous timetable intact
    with engine.connect() as conn:
        runs = conn.execute(
            select(IngestionRun.status, IngestionRun.error).order_by(IngestionRun.id)
        ).all()
    assert [r.status for r in runs] == ["success", "failed"]
    assert "NOPE" in runs[1].error
