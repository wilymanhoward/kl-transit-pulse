"""API tests against the test database, using the scenario in tests/scenario.py
(Friday 2026-11-06, route R1 S1 -> S2, buses A on time, B late, C unseen departure)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app import api as api_module
from app.cache import TTLCache
from app.estimate import run_estimation
from app.headways import Window
from app.queries import grid_times
from tests.scenario import kl


@pytest.fixture
def estimated(api: TestClient, day_of_data, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Scenario estimated at 12:05 KL; the API's clock is frozen at 10:05 KL by default."""
    run_estimation(day_of_data, days=1, now=kl(12, 5))
    monkeypatch.setattr(api_module, "utc_now", lambda: kl(10, 5))
    return api


def assert_error(resp, status: int) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"} and body["error"]["status"] == status
    return body["error"]


# ---------- /routes/{route_id}/reliability ----------


def test_reliability_all_hours(estimated: TestClient) -> None:
    body = estimated.get("/routes/R1/reliability").json()
    assert body["route"] == {"route_id": "R1", "short_name": "100", "long_name": "Alpha ~ Beta"}
    assert body["day_type"] == "weekday"  # default from the (frozen) Friday clock
    by_hour = {h["hour"]: h for h in body["hours"]}
    assert set(by_hour) == {10, 11}
    h10 = by_hour[10]
    assert h10["coverage"] == {"scheduled_departures": 2, "runs_seen": 2, "coverage_pct": 100.0}
    assert h10["delay"]["sample_size"] == 2 and h10["delay"]["on_time_pct"] == 50
    assert h10["headway"]["sample_size"] == 2
    assert by_hour[11]["headway"] is None  # 50% coverage: not judged
    assert len(body["notes"]) == 2  # estimate + coverage caveats always present


def test_reliability_single_hour_and_empty_hour(estimated: TestClient) -> None:
    assert [h["hour"] for h in estimated.get("/routes/R1/reliability?hour=10").json()["hours"]] == [
        10
    ]
    assert (
        estimated.get("/routes/R1/reliability?hour=3").json()["hours"] == []
    )  # known route, no data
    assert estimated.get("/routes/R1/reliability?day_type=weekend").json()["hours"] == []


def test_reliability_unknown_route_is_404(estimated: TestClient) -> None:
    err = assert_error(estimated.get("/routes/NOPE/reliability"), 404)
    assert "NOPE" in err["message"]


@pytest.mark.parametrize(
    ("url", "field"),
    [
        ("/routes/R1/reliability?hour=24", "hour"),
        ("/routes/R1/reliability?hour=-1", "hour"),
        ("/routes/R1/reliability?hour=six", "hour"),
        ("/routes/R1/reliability?day_type=holiday", "day_type"),
        ("/routes/R1;DROP/reliability", "route_id"),
        ("/routes/worst?limit=0", "limit"),
        ("/routes/worst?limit=51", "limit"),
        ("/routes/worst?offset=-1", "offset"),
        ("/routes/worst?min_buses=0", "min_buses"),
        ("/stops/S2/next?limit=100", "limit"),
        ("/stops/" + "x" * 33 + "/next", "stop_id"),
    ],
)
def test_bad_input_is_422_with_field(estimated: TestClient, url: str, field: str) -> None:
    err = assert_error(estimated.get(url), 422)
    assert err["message"] == "invalid request"
    assert [d["field"] for d in err["details"]] == [field]


# ---------- /routes/worst ----------


def test_worst_routes_min_sample_filter(estimated: TestClient) -> None:
    # R1 at 10:00 has 2 bus runs: hidden by the default min_buses=5 ...
    body = estimated.get("/routes/worst?hour=10").json()
    assert body["total"] == 0 and body["items"] == []
    # ... shown when the threshold is lowered.
    body = estimated.get("/routes/worst?hour=10&min_buses=2").json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["rank"] == 1 and item["route"]["route_id"] == "R1"
    assert item["on_time_pct"] == 50 and item["delay_buses"] == 2


def test_worst_routes_defaults_to_current_hour_and_paginates(estimated: TestClient) -> None:
    body = estimated.get("/routes/worst?min_buses=1").json()
    assert (body["hour"], body["day_type"]) == (10, "weekday")  # clock frozen at 10:05 Fri
    page2 = estimated.get("/routes/worst?min_buses=1&offset=1").json()
    assert page2["total"] == 1 and page2["items"] == [] and page2["offset"] == 1


def test_worst_routes_is_cached(estimated: TestClient, day_of_data) -> None:
    first = estimated.get("/routes/worst?hour=10&min_buses=2").json()
    with day_of_data.begin() as conn:
        conn.exec_driver_sql("DELETE FROM route_stats_hourly")
    assert estimated.get("/routes/worst?hour=10&min_buses=2").json() == first  # within TTL
    api_module.stats_cache.clear()
    assert estimated.get("/routes/worst?hour=10&min_buses=2").json()["total"] == 0


# ---------- /stops/{stop_id}/next ----------


def test_next_buses_live_and_scheduled(estimated: TestClient) -> None:
    body = estimated.get("/stops/S2/next").json()  # 10:05 KL
    assert body["stop"]["name"] == "BETA"

    # Bus A left S1 at 10:00 and is on its way: due 10:00 + 10.5 min = 10:10:30.
    [bus] = body["live"]
    assert bus["vehicle_id"] == "A" and bus["route"]["short_name"] == "100"
    assert bus["current_delay_secs"] is None  # only passed the first stop so far
    assert datetime.fromisoformat(bus["estimated_arrival"]) == kl(10, 10, 30)
    assert bus["estimated_arrival"].endswith("+08:00")  # all API times are in KL time
    assert bus["minutes_away"] == 5.5

    # Timetable grid for the next 90 min (R1 every 30 min from 06:00, +10.5 min to S2).
    times = [datetime.fromisoformat(s["scheduled_arrival"]) for s in body["scheduled"]]
    assert times == [kl(10, 10, 30), kl(10, 40, 30), kl(11, 10, 30)]
    first = body["scheduled"][0]
    assert first["scheduled_headway_secs"] == 1800
    # Buses leaving at 10:00-11:00 were measured at S2 twice (A and B): a typical delay.
    assert first["typical_delay_sample_size"] == 2 and first["typical_delay_secs"] > 0
    assert body["scheduled"][2]["typical_delay_sample_size"] == 0  # 11:00 departures: none


def test_next_buses_skips_bus_that_already_passed(estimated: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(api_module, "utc_now", lambda: kl(10, 40))
    live = estimated.get("/stops/S2/next").json()["live"]
    # A passed S2 at ~10:11 (and went quiet); B left at 10:30 and is approaching.
    assert [b["vehicle_id"] for b in live] == ["B"]


def test_next_buses_respects_limit(estimated: TestClient) -> None:
    assert len(estimated.get("/stops/S2/next?limit=1").json()["scheduled"]) == 1


def test_next_buses_unknown_stop_is_404(estimated: TestClient) -> None:
    assert_error(estimated.get("/stops/NOPE/next"), 404)


# ---------- error format everywhere ----------


def test_unknown_path_uses_error_format(estimated: TestClient) -> None:
    assert_error(estimated.get("/no/such/thing"), 404)


def test_database_down_is_503_without_internals(api: TestClient) -> None:
    dead = create_engine(
        "postgresql+psycopg://x:x@127.0.0.1:1/x", connect_args={"connect_timeout": 1}
    )
    api_module.app.dependency_overrides[api_module.get_engine] = lambda: dead
    err = assert_error(api.get("/routes/R1/reliability"), 503)
    assert err["message"] == "database unavailable"  # no SQL or hostnames leaked


def test_openapi_documents_endpoints(api: TestClient) -> None:
    paths = api.get("/openapi.json").json()["paths"]
    assert {
        "/health",
        "/routes/{route_id}/reliability",
        "/routes/worst",
        "/stops/{stop_id}/next",
    } <= set(paths)


# ---------- pure helpers (no database) ----------


def test_grid_times_shifts_departures_to_the_stop() -> None:
    midnight_kl = datetime(2026, 11, 5, 16, 0, tzinfo=UTC)  # 00:00 KL on 6 Nov
    got = list(
        grid_times([Window(6 * 3600, 7 * 3600, 1200)], 630, midnight_kl, kl(6, 15), kl(7, 0))
    )
    # Departures 06:00, 06:20, 06:40 (end exclusive) + 10:30 travel -> 06:10:30 is before
    # the 06:15 lower bound; 06:30:30 and 06:50:30 are inside.
    assert got == [(kl(6, 30, 30), 1200), (kl(6, 50, 30), 1200)]


def test_ttl_cache_expires() -> None:
    now = [0.0]
    cache = TTLCache(ttl_seconds=10, clock=lambda: now[0])
    calls = []
    compute = lambda: calls.append(1) or len(calls)  # noqa: E731
    assert cache.get_or_set("k", compute) == 1
    now[0] = 9.9
    assert cache.get_or_set("k", compute) == 1  # still fresh
    now[0] = 10.0
    assert cache.get_or_set("k", compute) == 2  # expired: recomputed


def test_ttl_cache_does_not_cache_errors() -> None:
    cache = TTLCache(ttl_seconds=10)

    def boom():
        raise RuntimeError

    with pytest.raises(RuntimeError):
        cache.get_or_set("k", boom)
    assert cache.get_or_set("k", lambda: "ok") == "ok"
