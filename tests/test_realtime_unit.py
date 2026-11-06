"""Realtime logic that needs no database: decoding, HTTP handling, backoff, scheduling."""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from app.config import Settings
from app.realtime import FetchError, decode, fetch_feed
from app.worker import next_delay, static_due
from tests.feeds import RECORDED, RECORDED_FEED_TS, bus, make_feed

T0 = datetime(2026, 11, 6, 10, 0, 0, tzinfo=UTC)


# ---------- decode ----------


def test_decode_recorded_feed() -> None:
    snap = decode(RECORDED.read_bytes())
    assert snap.feed_timestamp == RECORDED_FEED_TS
    assert len(snap.rows) == 88 and snap.dropped == 0
    first = snap.rows[0]
    assert first["vehicle_id"] == "WUY9579"
    assert first["trip_id"] == "weekday_T2000_T200002_0"
    assert first["route_id"] == "T2000"
    assert first["trip_start_date"] == date(2026, 11, 5)
    assert first["trip_start_secs"] == 18 * 3600 + 5 * 60 + 15  # "18:05:15"
    assert first["latitude"] == pytest.approx(3.2521830)
    assert first["observed_at"] == datetime.fromtimestamp(1793874405, UTC)


def test_decode_drops_unusable_entities_and_tolerates_missing_fields() -> None:
    data = make_feed(
        T0,
        [
            bus("OK1", T0, speed=4.5),
            {"id": "NOPOS", "ts": T0},  # no position at all
            bus("ZERO", T0, lat=0.0, lon=0.0),  # "no GPS fix" placeholder
            {"lat": 3.1, "lon": 101.6, "ts": T0},  # no vehicle id or plate
            {"plate": "PLATEONLY", "lat": 3.1, "lon": 101.6},  # no own timestamp, no trip
            bus("BADTIME", T0, start_time="nonsense", start_date="2026-11-06"),
        ],
    )
    snap = decode(data)
    by_id = {r["vehicle_id"]: r for r in snap.rows}
    assert set(by_id) == {"OK1", "PLATEONLY", "BADTIME"}
    assert snap.dropped == 3
    assert by_id["OK1"]["speed_mps"] == pytest.approx(4.5)
    assert by_id["OK1"]["bearing"] is None  # unset stays NULL, not 0
    assert by_id["PLATEONLY"]["observed_at"] == T0  # falls back to header time
    assert by_id["PLATEONLY"]["trip_id"] is None
    assert by_id["BADTIME"]["trip_start_secs"] is None  # bad value -> NULL, row kept
    assert by_id["BADTIME"]["trip_start_date"] is None


@pytest.mark.parametrize("data", [b"\xff\xff\xff not protobuf", b""])
def test_decode_rejects_garbage(data: bytes) -> None:
    with pytest.raises(ValueError):
        decode(data)


# ---------- fetch (mocked HTTP) ----------


def client_returning(*responses: httpx.Response | Exception) -> tuple[httpx.Client, list]:
    """A client that replays the given responses/exceptions, recording each request."""
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_fetch_success_makes_exactly_one_request() -> None:
    client, seen = client_returning(httpx.Response(200, content=b"abc"))
    assert fetch_feed(client) == b"abc"
    assert len(seen) == 1
    assert seen[0].url.path.endswith("/prasarana/")  # trailing slash: no 301 hop
    assert seen[0].url.params["category"] == "rapid-bus-kl"


def test_fetch_429_reports_retry_after() -> None:
    client, _ = client_returning(httpx.Response(429, headers={"Retry-After": "90"}))
    with pytest.raises(FetchError) as err:
        fetch_feed(client)
    assert err.value.retry_after == 90


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(503),
        httpx.Response(500),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("refused"),
    ],
)
def test_fetch_server_and_network_errors_become_fetch_error(failure) -> None:
    client, _ = client_returning(failure)
    with pytest.raises(FetchError) as err:
        fetch_feed(client)
    assert err.value.retry_after is None


# ---------- backoff ----------


def test_backoff_doubles_then_caps() -> None:
    s = Settings(poll_seconds=30, max_backoff_seconds=300)
    assert [next_delay(n, s) for n in range(1, 6)] == [60, 120, 240, 300, 300]


def test_backoff_respects_longer_retry_after() -> None:
    s = Settings()
    assert next_delay(1, s, retry_after=200) == 200
    assert next_delay(3, s, retry_after=5) == 240  # our backoff is already longer


def test_healthy_delay_keeps_cadence_between_tick_starts() -> None:
    s = Settings(poll_seconds=30)
    assert next_delay(0, s, elapsed=4) == 26
    assert next_delay(0, s, elapsed=45) == 0  # slow tick: go again, never negative


def test_poll_interval_cannot_break_rate_limit() -> None:
    with pytest.raises(ValueError, match="4 requests/min"):
        Settings(poll_seconds=10)
    Settings(poll_seconds=15)  # exactly 4/min is allowed


# ---------- daily timetable refresh ----------

KL_OFFSET = timedelta(hours=8)


def kl(day: int, hour: int, minute: int = 0) -> datetime:
    """A Kuala Lumpur wall-clock time on 2026-11-<day>, as an aware UTC datetime."""
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC) - KL_OFFSET


@pytest.mark.parametrize(
    ("last", "now", "due"),
    [
        (None, kl(6, 12), True),  # never loaded
        (kl(6, 4, 5), kl(6, 12), False),  # loaded after today's 04:00
        (kl(6, 3, 59), kl(6, 12), True),  # loaded before today's 04:00
        (kl(5, 4, 30), kl(6, 3, 0), False),  # 03:00: yesterday's 04:30 load still current
        (kl(5, 4, 30), kl(6, 4, 0), True),  # 04:00 exactly: due again
    ],
)
def test_static_due(last, now, due) -> None:
    assert static_due(last, now) is due
