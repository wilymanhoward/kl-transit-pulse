"""Realtime ingestion against a real Postgres (the `engine` fixture skips without one)."""

from datetime import UTC, datetime, timedelta
from functools import partial
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, insert, select

from app.api import app, get_engine
from app.config import Settings
from app.models import IngestionRun, VehiclePosition
from app.realtime import delete_old_positions, fetch_feed, ingest_once
from app.worker import run_forever
from tests.feeds import RECORDED, RECORDED_FEED_TS, bus, make_feed

STALE = timedelta(minutes=5)
NOW = RECORDED_FEED_TS + timedelta(seconds=30)  # pretend we fetched it 30 s after publish


def positions(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(VehiclePosition)).scalar_one()


def runs(engine: Engine, kind: str = "realtime") -> list[tuple]:
    with engine.connect() as conn:
        return conn.execute(
            select(IngestionRun.status, IngestionRun.rows_written)
            .where(IngestionRun.kind == kind)
            .order_by(IngestionRun.id)
        ).all()


def test_ingest_stores_recorded_snapshot(engine: Engine) -> None:
    data = RECORDED.read_bytes()
    feed_ts = ingest_once(engine, lambda: data, None, STALE, NOW)
    assert feed_ts == RECORDED_FEED_TS
    assert positions(engine) == 88
    assert runs(engine) == [("success", 88)]


def test_repeated_snapshot_is_skipped(engine: Engine) -> None:
    data = RECORDED.read_bytes()
    feed_ts = ingest_once(engine, lambda: data, None, STALE, NOW)
    ingest_once(engine, lambda: data, feed_ts, STALE, NOW + timedelta(seconds=30))
    assert positions(engine) == 88
    assert runs(engine) == [("success", 88), ("skipped", 0)]


def test_stale_snapshot_is_skipped(engine: Engine) -> None:
    data = RECORDED.read_bytes()
    ingest_once(engine, lambda: data, None, STALE, RECORDED_FEED_TS + timedelta(minutes=6))
    assert positions(engine) == 0
    assert runs(engine) == [("skipped", 0)]


def test_same_gps_fix_in_new_snapshot_is_not_duplicated(engine: Engine) -> None:
    t = datetime(2026, 11, 6, 2, 0, tzinfo=UTC)
    first = make_feed(t, [bus("A", t), bus("B", t)])
    # 30 s later bus A hasn't sent a new fix (same GPS timestamp); B has moved on.
    second = make_feed(
        t + timedelta(seconds=30), [bus("A", t), bus("B", t + timedelta(seconds=25))]
    )
    ts = ingest_once(engine, lambda: first, None, STALE, t)
    ingest_once(engine, lambda: second, ts, STALE, t + timedelta(seconds=30))
    assert positions(engine) == 3
    assert runs(engine) == [("success", 2), ("success", 1)]


def test_failed_fetch_is_recorded_and_raised(engine: Engine) -> None:
    def boom() -> bytes:
        raise RuntimeError("feed down")

    with pytest.raises(RuntimeError):
        ingest_once(engine, boom, None, STALE, NOW)
    assert runs(engine) == [("failed", None)]


def test_retention_deletes_only_old_positions(engine: Engine) -> None:
    t = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
    old, recent = t - timedelta(days=15), t - timedelta(days=13)
    data = make_feed(t, [bus("OLD", old), bus("NEW", recent)])
    ingest_once(engine, lambda: data, None, timedelta(days=30), t)
    assert delete_old_positions(engine, retention_days=14, now=t) == 1
    with engine.connect() as conn:
        assert conn.execute(select(VehiclePosition.vehicle_id)).scalars().all() == ["NEW"]


def test_worker_survives_failures_and_backs_off(engine: Engine, mini_zip: bytes) -> None:
    """Mocked HTTP: 429, 503, timeout, then two identical good snapshots."""
    data = RECORDED.read_bytes()
    replies = [
        httpx.Response(429, headers={"Retry-After": "90"}),
        httpx.Response(503),
        httpx.ReadTimeout("timed out"),
        httpx.Response(200, content=data),
        httpx.Response(200, content=data),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        item = replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    stop, delays = Event(), []

    def fake_wait(seconds: float) -> None:
        delays.append(seconds)
        if not replies:
            stop.set()

    client = httpx.Client(transport=httpx.MockTransport(handler))
    run_forever(
        engine,
        partial(fetch_feed, client),
        lambda: mini_zip,
        Settings(poll_seconds=30, max_backoff_seconds=300),
        stop,
        wait=fake_wait,
        now=lambda: NOW,
    )

    assert delays[:3] == [90, 120, 240]  # Retry-After beat 60; then 2x, 4x
    assert delays[3] == pytest.approx(30, abs=5) and delays[4] == pytest.approx(30, abs=5)
    assert [r[0] for r in runs(engine)] == ["failed", "failed", "failed", "success", "skipped"]
    assert runs(engine, "static") == [("success", 19)]  # timetable loaded once on start
    assert positions(engine) == 88


def test_worker_survives_database_outage(mini_zip: bytes) -> None:
    """If Postgres is down, every job fails, but the loop keeps going and backs off."""
    from sqlalchemy import create_engine

    dead = create_engine(
        "postgresql+psycopg://x:x@127.0.0.1:1/x", connect_args={"connect_timeout": 1}
    )
    stop, delays = Event(), []

    def fake_wait(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) == 3:
            stop.set()

    run_forever(dead, lambda: b"", lambda: mini_zip, Settings(), stop, wait=fake_wait)
    assert delays == [60, 120, 240]


# ---------- /health ----------


@pytest.fixture
def api(engine: Engine):
    app.dependency_overrides[get_engine] = lambda: engine
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health_unhealthy_when_nothing_ingested(api: TestClient) -> None:
    resp = api.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert len(body["problems"]) == 3


def test_health_ok_with_fresh_data(api: TestClient, engine: Engine, mini_zip: bytes) -> None:
    from app.gtfs_static import import_static

    import_static(engine, lambda: mini_zip)
    now = datetime.now(UTC).replace(microsecond=0)
    data = make_feed(now, [bus("A", now - timedelta(seconds=40))])
    ingest_once(engine, lambda: data, None, STALE, now)

    resp = api.get("/health")
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["status"] == "ok" and body["problems"] == []
    assert body["positions_last_hour"] == 1


def test_health_catches_frozen_feed(api: TestClient, engine: Engine) -> None:
    """Runs keep 'succeeding' but the newest GPS fix is old: still unhealthy."""
    old = datetime.now(UTC) - timedelta(hours=2)
    with engine.begin() as conn:
        run_id = conn.execute(
            insert(IngestionRun)
            .values(kind="realtime", status="success", finished_at=func.now())
            .returning(IngestionRun.id)
        ).scalar_one()
        conn.execute(
            insert(VehiclePosition).values(
                ingestion_run_id=run_id,
                vehicle_id="A",
                observed_at=old,
                latitude=3.1,
                longitude=101.6,
            )
        )
    body = api.get("/health").json()
    assert body["status"] == "unhealthy"
    assert any("newest vehicle position" in p for p in body["problems"])
    assert not any("realtime ingestion" in p for p in body["problems"])
