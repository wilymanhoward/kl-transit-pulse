from datetime import date

import pytest

from app.gtfs_static import parse_feed, parse_gtfs_time
from app.models import Calendar, Frequency, Route, Stop, StopTime, Trip
from tests.conftest import FIXTURE_DIR, make_zip


@pytest.mark.parametrize(
    ("text", "secs"),
    [
        ("00:00:00", 0),
        ("06:10:30", 22230),
        ("7:05:09", 25509),  # GTFS allows a one-digit hour
        ("24:10:00", 87000),  # past midnight, same service day
        ("25:15:00", 90900),  # the max seen in the real rapid-bus-kl feed
    ],
)
def test_parse_gtfs_time(text: str, secs: int) -> None:
    assert parse_gtfs_time(text) == secs


@pytest.mark.parametrize("text", ["25:15", "aa:00:00", "10:60:00", "10:00:60", "-1:00:00", ""])
def test_parse_gtfs_time_rejects_bad_input(text: str) -> None:
    with pytest.raises(ValueError):
        parse_gtfs_time(text)


def test_parse_feed_counts(mini_zip: bytes) -> None:
    tables = parse_feed(mini_zip)
    counts = {model: len(rows) for model, rows in tables.items()}
    assert counts == {Route: 2, Stop: 3, Calendar: 2, Trip: 3, StopTime: 6, Frequency: 3}


def test_parse_feed_converts_types(mini_zip: bytes) -> None:
    tables = parse_feed(mini_zip)

    late = [st for st in tables[StopTime] if st["trip_id"] == "weekday_R2_R202_0"]
    assert [st["arrival_secs"] for st in late] == [85800, 87000]  # 23:50, 24:10

    weekday = next(c for c in tables[Calendar] if c["service_id"] == "weekday")
    assert weekday["friday"] is True and weekday["saturday"] is False
    assert weekday["start_date"] == date(2020, 4, 1)

    freq = next(f for f in tables[Frequency] if f["trip_id"] == "weekday_R2_R202_0")
    assert (freq["start_secs"], freq["end_secs"], freq["headway_secs"]) == (85800, 90000, 900)
    assert freq["exact_times"] is False

    stop3 = next(s for s in tables[Stop] if s["stop_id"] == "S3")
    assert stop3["stop_desc"] == "GAMMA, NEAR MALL"  # quoted comma survives CSV parsing
    assert stop3["stop_lat"] == pytest.approx(3.12)


def test_empty_fields_become_none(mini_zip: bytes) -> None:
    tables = parse_feed(mini_zip)
    assert next(s for s in tables[Stop] if s["stop_id"] == "S2")["stop_desc"] is None
    assert next(r for r in tables[Route] if r["route_id"] == "R2")["route_color"] is None
    assert tables[Trip][0]["trip_headsign"] is None
    assert tables[Trip][2]["direction_id"] == 1


def test_missing_required_file_is_a_clear_error() -> None:
    with pytest.raises(ValueError, match="frequencies.txt"):
        parse_feed(make_zip({"frequencies.txt": None}))


def test_byte_order_mark_is_ignored() -> None:
    routes = (FIXTURE_DIR / "routes.txt").read_text(encoding="utf-8")
    tables = parse_feed(make_zip({"routes.txt": "﻿" + routes}))
    assert tables[Route][0]["route_id"] == "R1"  # not "﻿route_id" key error
