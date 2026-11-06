"""End-to-end estimation on the scenario in tests/scenario.py (needs the test database)."""

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.estimate import run_estimation
from app.models import RouteCoverage, RouteStatsHourly, StopArrival
from tests.scenario import kl


def arrivals(engine: Engine) -> dict[tuple[str, str], dict]:
    with Session(engine) as session:
        rows = session.scalars(select(StopArrival)).all()
    return {(r.vehicle_id, r.stop_id): r for r in rows}


def test_delay_headway_and_fallback_stop_list(day_of_data: Engine) -> None:
    run_estimation(day_of_data, days=1, now=kl(12, 5))
    arr = arrivals(day_of_data)
    assert set(arr) == {
        ("A", "S1"),
        ("A", "S2"),
        ("B", "S1"),
        ("B", "S2"),
        ("C", "S1"),
        ("C", "S2"),
    }

    # First stop is the departure itself: no delay judged there.
    assert arr[("A", "S1")].delay_secs is None
    # S2 is 1.57 km away, scheduled 630 s. A reaches the S2 pin at 706 s (76 s late), B
    # at 1471 s (841 s late). Arrival = first moment within 60 m of the stop, so the
    # estimate is up to radius / speed earlier: ~26 s for A, ~55 s for B at these slow
    # test speeds (~7 s for a real bus at 8 m/s).
    assert 76 - 30 <= arr[("A", "S2")].delay_secs <= 76
    assert arr[("A", "S2")].on_time is True
    assert 841 - 60 <= arr[("B", "S2")].delay_secs <= 841
    assert arr[("B", "S2")].on_time is False
    # C borrowed the R101 stop list, but its departure wasn't seen: no delay.
    assert arr[("C", "S2")].delay_secs is None

    # Headway at S2: A ~10:12, B ~10:55 -> ~43 min against a 30 min schedule.
    assert arr[("A", "S2")].headway_secs is None  # first bus seen that day
    assert arr[("B", "S2")].headway_secs == pytest.approx(43 * 60, abs=60)
    assert arr[("B", "S2")].scheduled_headway_secs == 1800
    assert arr[("B", "S2")].regular is False  # over 30 + 5 min


def test_coverage_and_gated_stats(day_of_data: Engine) -> None:
    run_estimation(day_of_data, days=1, now=kl(12, 5))
    with Session(day_of_data) as conn:
        cov = {
            r.hour: r
            for r in conn.scalars(select(RouteCoverage).where(RouteCoverage.route_id == "R1"))
        }
        stats = {
            r.hour: r
            for r in conn.scalars(select(RouteStatsHourly).where(RouteStatsHourly.route_id == "R1"))
        }

    assert (cov[10].scheduled_departures, cov[10].runs_seen, cov[10].ingestion_complete) == (
        2,
        2,
        True,
    )
    assert (cov[11].scheduled_departures, cov[11].runs_seen, cov[11].ingestion_complete) == (
        2,
        1,
        True,
    )
    assert cov[9].ingestion_complete is False  # worker wasn't polling yet

    h10 = stats[10]
    assert h10.day_type == "weekday"
    assert h10.coverage_pct == 100
    assert (h10.delay_sample_size, h10.delay_buses, h10.on_time_pct) == (2, 2, 50)
    # 100% coverage: headways count. B's gaps at S1 (30 min, regular) and S2 (43, not).
    assert h10.headway_sample_size == 2 and h10.regular_pct == 50

    h11 = stats[11]
    assert h11.coverage_pct == 50
    assert h11.headway_sample_size == 0 and h11.regular_pct is None  # gated out (< 80%)
    assert h11.delay_sample_size == 0  # C's departure wasn't seen

    # Hour 9 was scheduled but we weren't polling: no stats row at all, rather than a
    # misleading "0% of buses seen".
    assert 9 not in stats


def test_rerunning_replaces_rather_than_duplicates(day_of_data: Engine) -> None:
    first = run_estimation(day_of_data, days=1, now=kl(12, 5))
    second = run_estimation(day_of_data, days=1, now=kl(12, 5))
    assert first == second
    assert len(arrivals(day_of_data)) == 6
