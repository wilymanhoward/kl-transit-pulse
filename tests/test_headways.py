"""Pure headway logic: the scenarios from the brief, on a synthetic straight road.

The road runs east along latitude 3.1. Stops sit every 300 m (a realistic spacing:
the real median is 343 m) and buses report every 30 s.
"""

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from app.headways import (
    Fix,
    StopPoint,
    Window,
    assign_headways,
    closest_on_segment,
    detect_arrivals,
    distance_m,
    is_regular,
    scheduled_headway,
)

LAT = 3.1
LON0 = 101.6
T0 = datetime(2026, 11, 6, 2, 0, tzinfo=UTC)  # 10:00 KL


def east(m: float, north: float = 0.0) -> tuple[float, float]:
    """(lat, lon) `m` metres east and `north` metres north of the road's origin."""
    lat = LAT + math.degrees(north / 6_371_000)
    lon = LON0 + math.degrees(m / (6_371_000 * math.cos(math.radians(LAT))))
    return lat, lon


def stop(seq: int, metres: float, stop_id: str | None = None) -> StopPoint:
    return StopPoint(seq, stop_id or f"S{seq}", *east(metres))


def fix(seconds: float, metres: float, north: float = 0.0) -> Fix:
    return Fix(T0 + timedelta(seconds=seconds), *east(metres, north))


STOPS = [stop(i + 1, i * 300) for i in range(5)]  # S1..S5 at 0, 300, ..., 1200 m


def drive(speed_mps: float = 10, every_s: int = 30, until_m: float = 1300, start_s: float = 0):
    """Fixes for a bus driving east at constant speed from the first stop."""
    fixes, t = [], 0.0
    while t * speed_mps <= until_m:
        fixes.append(fix(start_s + t, t * speed_mps))
        t += every_s
    return fixes


# ---------- geometry ----------


def test_distance_is_metres() -> None:
    assert distance_m(*east(0), *east(300)) == pytest.approx(300, rel=1e-3)


def test_closest_point_on_segment() -> None:
    frac, dist = closest_on_segment(fix(0, 0, north=20), fix(30, 300, north=20), stop(1, 150))
    assert frac == pytest.approx(0.5, abs=1e-3)
    assert dist == pytest.approx(20, abs=0.1)


def test_closest_point_clamps_to_segment_ends() -> None:
    frac, dist = closest_on_segment(fix(0, 0), fix(30, 100), stop(1, 400))
    assert frac == 1.0 and dist == pytest.approx(300, rel=1e-3)


# ---------- arrival detection ----------


def test_bus_passing_stops_between_fixes_gets_interpolated_times() -> None:
    # 10 m/s, fixes every 30 s = 300 m apart, offset so no fix lands on a stop.
    fixes = [fix(t, 10 * t - 150) for t in range(0, 181, 30)]
    arrivals = detect_arrivals(fixes, STOPS)
    assert [a.stop_id for a in arrivals] == ["S1", "S2", "S3", "S4", "S5"]
    # Stop k is at 300k m, reached at t = (300k + 150) / 10 s.
    for k, arr in enumerate(arrivals):
        assert (arr.t - T0).total_seconds() == pytest.approx(30 * k + 15, abs=0.5)


def test_several_stops_in_one_long_segment_come_out_in_order() -> None:
    fixes = [fix(0, -10), fix(60, 1210)]  # one 60 s hop past all five stops
    assert [a.stop_id for a in detect_arrivals(fixes, STOPS)] == ["S1", "S2", "S3", "S4", "S5"]


def test_missed_stop_is_left_out_not_invented() -> None:
    # The bus detours 200 m north around S3, then rejoins the road.
    fixes = [fix(0, -50), fix(30, 250), fix(60, 450, north=200), fix(90, 650, north=200),
             fix(120, 850), fix(150, 1250)]  # fmt: skip
    assert [a.stop_id for a in detect_arrivals(fixes, STOPS)] == ["S1", "S2", "S4", "S5"]


def test_gps_jitter_at_a_stop_gives_one_arrival() -> None:
    # Bus dwells at S2 for 3 minutes, GPS wandering +-15 m, then drives on.
    dwell = [fix(30 + 15 * i, 300 + (15 if i % 2 else -15), north=10) for i in range(12)]
    fixes = [fix(0, -30), *dwell, fix(240, 600), fix(270, 900)]
    arrivals = detect_arrivals(fixes, STOPS)
    assert [a.stop_id for a in arrivals] == ["S1", "S2", "S3", "S4"]
    s2 = arrivals[1]
    assert (s2.t - T0).total_seconds() < 50  # first time it got near, not when it left


def test_loop_route_start_and_end_at_same_stop() -> None:
    # Out along the road and back to the start: A, B, C, B', A (the end reuses stop ids).
    loop = [stop(1, 0, "A"), stop(2, 300, "B"), stop(3, 600, "C"),
            stop(4, 300, "B"), stop(5, 0, "A")]  # fmt: skip
    out_and_back = [fix(t, 10 * t - 50) for t in range(0, 70, 30)]  # -50 .. 550
    back = [fix(90, 650), fix(120, 450), fix(150, 150), fix(180, -150)]
    arrivals = detect_arrivals(out_and_back + back, loop)
    assert [(a.sequence, a.stop_id) for a in arrivals] == [
        (1, "A"), (2, "B"), (3, "C"), (4, "B"), (5, "A")]  # fmt: skip
    # Sequence order means stop 5 (A again) only matches at the end of the trip.
    assert arrivals[4].t > arrivals[3].t > arrivals[2].t


def test_bus_waiting_at_loop_terminal_anchors_to_first_stop() -> None:
    loop = [stop(1, 0, "A"), stop(2, 300, "B"), stop(3, 0, "A")]
    fixes = [fix(0, 5), fix(30, 5), fix(60, 200), fix(90, 400)]
    assert [a.sequence for a in detect_arrivals(fixes, loop)] == [1, 2]


def test_run_first_seen_mid_route_starts_at_nearest_stop() -> None:
    # Worker started while the bus was already near S3.
    fixes = [fix(0, 590), fix(30, 890), fix(60, 1190)]
    assert [a.stop_id for a in detect_arrivals(fixes, STOPS)] == ["S3", "S4", "S5"]


def test_long_gap_between_fixes_is_not_interpolated() -> None:
    # GPS silent for 5 minutes between 250 m and 1150 m: S3 and S4 passed unseen.
    fixes = [fix(0, -50), fix(30, 250), fix(330, 1150), fix(360, 1250)]
    arrivals = detect_arrivals(fixes, STOPS, max_gap_s=180)
    assert [a.stop_id for a in arrivals] == ["S1", "S2", "S5"]


def test_track_far_from_route_finds_nothing() -> None:
    fixes = [fix(0, 0, north=500), fix(30, 300, north=500), fix(60, 600, north=500)]
    assert detect_arrivals(fixes, STOPS) == []


@pytest.mark.parametrize("fixes", [[], [fix(0, 0)]])
def test_too_few_fixes(fixes) -> None:
    assert detect_arrivals(fixes, STOPS) == []


# ---------- schedule ----------

WINDOWS = [Window(21600, 25200, 1200), Window(27000, 36000, 1800)]  # 06-07 @20m, 07:30-10 @30m


@pytest.mark.parametrize(
    ("departure", "expected"),
    [(21600, 1200), (25200, 1200), (30000, 1800), (26000, None), (90000, None), (None, None)],
)
def test_scheduled_headway(departure, expected) -> None:
    assert scheduled_headway(WINDOWS, departure) == expected


def test_overlapping_windows_combine() -> None:
    # Two patterns each every 30 min, both running: a bus every 15 min.
    assert scheduled_headway([Window(0, 3600, 1800), Window(0, 3600, 1800)], 100) == 900


# ---------- headways ----------


def arr(minute: float, seq: int = 2, day: date = date(2026, 11, 6), shape: str = "P1") -> dict:
    return {
        "shape_id": shape,
        "stop_sequence": seq,
        "service_date": day,
        "arrived_at": T0 + timedelta(minutes=minute),
    }


def test_headways_on_time_late_and_bunched() -> None:
    rows = assign_headways([arr(0), arr(20), arr(55), arr(58)])  # scheduled every 20 min
    assert [r["headway_secs"] for r in rows] == [None, 1200, 2100, 180]
    assert is_regular(1200, 1200)  # on time
    assert not is_regular(2100, 1200)  # 15 min over: a long wait
    assert is_regular(180, 1200)  # bunched, but nobody waited long


def test_headways_are_per_stop_sequence_pattern_and_day() -> None:
    rows = assign_headways(
        [
            arr(0, seq=2),
            arr(10, seq=3),
            arr(20, seq=2, shape="P2"),
            arr(30, seq=2),
            arr(40, seq=2, day=date(2026, 11, 7)),
        ]  # fmt: skip
    )
    assert [r["headway_secs"] for r in rows] == [None, None, None, 1800, None]


def test_headway_across_midnight_stays_in_service_day() -> None:
    # Service day 6 Nov: buses at 23:50 and 00:20 (still 6 Nov's service, after midnight).
    late = assign_headways([arr(13 * 60 + 50), arr(14 * 60 + 20)])  # T0 is 10:00 KL
    assert late[1]["headway_secs"] == 1800


def test_headway_spanning_an_outage_is_unknown() -> None:
    outage = [(T0 + timedelta(minutes=25), T0 + timedelta(minutes=40))]
    rows = assign_headways([arr(0), arr(20), arr(50), arr(70)], outage)
    assert [r["headway_secs"] for r in rows] == [None, 1200, None, 1200]


def test_unsorted_input_is_sorted_by_time() -> None:
    rows = assign_headways([arr(40), arr(0), arr(20)])
    assert sorted(r["headway_secs"] or 0 for r in rows) == [0, 1200, 1200]


@pytest.mark.parametrize(
    ("headway", "scheduled", "regular"),
    [(780, 600, True), (781, 600, False), (2100, 1800, True), (2101, 1800, False)],
)
def test_wait_assessment_allowance(headway, scheduled, regular) -> None:
    assert is_regular(headway, scheduled) is regular
