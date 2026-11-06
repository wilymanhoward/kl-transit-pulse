"""Pure reliability estimation: GPS fixes + stop list in, stop arrivals, delays and
headways out.

No I/O here, so every edge case is unit-testable. app/estimate.py does the database side.

These are ESTIMATES from GPS, not official figures. Two measures:
  * Delay (main): the realtime feed's trip start_time is the run's actual departure, and
    stop_times gives the scheduled travel time to each stop. delay = arrival -
    (departure + scheduled travel time). Each bus is judged on its own, so the ~70% of
    buses missing from the feed don't distort it.
  * Headway (only where coverage is high): the gap between consecutive buses at a stop
    vs the scheduled gap. A missing bus looks like a long gap, so it's only trusted for
    route-hours where we saw most of the scheduled runs (see app/estimate.py).

Arrival detection, per bus run:
  1. Anchor: start at the stop nearest the run's first fix (earliest one if tied, so a
     loop route starts at its first stop, not its identical last stop).
  2. For each pair of consecutive fixes (A -> B), look at the next few stops in order.
     If the straight segment A-B passes within `radius_m` of a stop, the bus passed it.
     Arrival time is interpolated along the segment (fixes are ~300 m apart, stops
     ~340 m apart, so the bus often passes a stop between two fixes).
  3. Only move forward through the stop list, one arrival per stop sequence. This
     handles GPS jitter at a stop, loops, and routes that visit a stop twice.
  4. A gap between fixes longer than `max_gap_s` makes interpolation meaningless: skip
     that segment and re-anchor on the nearest stop still ahead.
"""

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

EARTH_RADIUS_M = 6_371_000
KL = ZoneInfo("Asia/Kuala_Lumpur")


@dataclass(frozen=True)
class Fix:
    t: datetime
    lat: float
    lon: float


@dataclass(frozen=True)
class StopPoint:
    sequence: int
    stop_id: str
    lat: float
    lon: float
    offset_secs: int = 0  # scheduled travel time from the trip's first stop


@dataclass(frozen=True)
class Arrival:
    sequence: int
    stop_id: str
    t: datetime
    distance_m: float  # closest approach of the GPS track to the stop


@dataclass(frozen=True)
class Window:
    """One frequencies.txt row: departures every headway_secs between start and end."""

    start_secs: int
    end_secs: int
    headway_secs: int


# ---------- geometry ----------


def _to_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Metres east/north of (lat0, lon0). Flat-earth is accurate to <0.1% over a few km."""
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * EARTH_RADIUS_M
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return x, y


def distance_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    return math.hypot(*_to_xy(b_lat, b_lon, a_lat, a_lon))


def closest_on_segment(a: Fix, b: Fix, stop: StopPoint) -> tuple[float, float]:
    """(fraction 0..1 along A->B of the point nearest the stop, distance in metres)."""
    ax, ay = _to_xy(a.lat, a.lon, stop.lat, stop.lon)
    bx, by = _to_xy(b.lat, b.lon, stop.lat, stop.lon)
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    # Stop is the origin, so the nearest point solves for t in A + t(B - A).
    frac = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
    return frac, math.hypot(ax + frac * dx, ay + frac * dy)


def _anchor(fix: Fix, stops: list[StopPoint], start: int) -> int:
    """Index (>= start) of the stop nearest `fix`; earliest wins within 5 m of a tie."""
    dists = [distance_m(fix.lat, fix.lon, s.lat, s.lon) for s in stops[start:]]
    best = min(dists)
    return start + next(i for i, d in enumerate(dists) if d <= best + 5)


# ---------- arrivals ----------


def detect_arrivals(
    fixes: list[Fix],
    stops: list[StopPoint],
    radius_m: float = 60.0,
    lookahead: int = 8,
    max_gap_s: float = 180.0,
) -> list[Arrival]:
    """Estimate when one bus run passed each stop. `fixes` must be sorted by time and
    `stops` by sequence. Stops the track never comes near are left out (missed)."""
    if len(fixes) < 2 or not stops:
        return []
    # A stop at the same place as an earlier one (a loop's last stop, the way back on an
    # out-and-back route) may only match when it's the very next stop, never by skipping
    # ahead; otherwise a bus still on its way out would "arrive" at its return stops.
    revisit = [
        any(distance_m(s.lat, s.lon, e.lat, e.lon) <= radius_m for e in stops[:j])
        for j, s in enumerate(stops)
    ]
    arrivals: list[Arrival] = []
    nxt = _anchor(fixes[0], stops, 0)  # index of the next stop we're waiting for

    for a, b in pairwise(fixes):
        if nxt >= len(stops):
            break
        dt = (b.t - a.t).total_seconds()
        if dt > max_gap_s:
            nxt = _anchor(b, stops, nxt)
            continue
        last_frac = -1.0  # several stops can be passed in one segment, strictly in order
        for j in range(nxt, min(nxt + lookahead, len(stops))):
            if revisit[j] and j != nxt:
                continue
            frac, dist = closest_on_segment(a, b, stops[j])
            if dist <= radius_m and frac > last_frac:
                stop = stops[j]
                t = a.t + (b.t - a.t) * frac
                arrivals.append(Arrival(stop.sequence, stop.stop_id, t, round(dist, 1)))
                nxt, last_frac = j + 1, frac
    return arrivals


# ---------- schedule ----------


def scheduled_headway(windows: Iterable[Window], departure_secs: int | None) -> int | None:
    """Scheduled gap (s) for a run departing at `departure_secs` (seconds after midnight of
    its service day). Overlapping windows combine like frequencies: 1/h = 1/h1 + 1/h2.
    None if no window covers that time (a gap in the timetable: nothing to judge)."""
    if departure_secs is None:
        return None
    active = [w for w in windows if w.start_secs <= departure_secs <= w.end_secs]
    if not active:
        return None
    return round(1 / sum(1 / w.headway_secs for w in active))


# ---------- headways ----------


def assign_headways(
    arrivals: list[dict], outages: list[tuple[datetime, datetime]] = ()
) -> list[dict]:
    """Add `headway_secs` to each arrival dict: time since the previous bus of the same
    pattern at the same stop on the same service day. Needs keys shape_id, stop_sequence,
    service_date, arrived_at.

    headway_secs is None for the first bus of the day and when an ingestion outage falls
    between the two buses (a bus might have passed unseen, so the gap can't be trusted).
    Grouping by stop *sequence* (not just stop_id) keeps a loop's start and end apart.
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for arr in arrivals:
        groups[(arr["shape_id"], arr["stop_sequence"], arr["service_date"])].append(arr)
    for group in groups.values():
        group.sort(key=lambda r: r["arrived_at"])
        group[0]["headway_secs"] = None
        for prev, cur in pairwise(group):
            unseen = any(
                start < cur["arrived_at"] and end > prev["arrived_at"] for start, end in outages
            )
            gap = (cur["arrived_at"] - prev["arrived_at"]).total_seconds()
            cur["headway_secs"] = None if unseen else round(gap)
    return arrivals


def is_regular(headway_secs: int, scheduled_secs: int) -> bool:
    """MTA-style 'wait assessment': a gap is acceptable if it's no more than 3 min over
    schedule for frequent service (<= 10 min), or 5 min over for less frequent service."""
    allowance = 180 if scheduled_secs <= 600 else 300
    return headway_secs <= scheduled_secs + allowance


# ---------- delay (per bus, against its own departure) ----------

ON_TIME_EARLY_SECS = 60  # common transit definition: <= 1 min early ...
ON_TIME_LATE_SECS = 300  # ... and <= 5 min late
DEPARTURE_SEEN_WINDOW = (-60, 180)  # first fix this close to start_time = departure seen


def service_midnight(service_date: date) -> datetime:
    return datetime.combine(service_date, datetime.min.time(), KL)


def service_day_secs(t: datetime, service_date: date) -> float:
    """Seconds since midnight (KL) at the start of the service day. A bus at 00:20 on
    the next calendar day, still running the previous day's service, gives 87,600."""
    return (t - service_midnight(service_date)).total_seconds()


def departure_seen(first_fix: datetime, service_date: date, start_secs: int | None) -> bool:
    """Was the run's first GPS fix close to its start_time? Otherwise start_time may not
    be when this bus really left (10% of runs are first seen an hour or more later)."""
    if start_secs is None:
        return False
    lag = service_day_secs(first_fix, service_date) - start_secs
    return DEPARTURE_SEEN_WINDOW[0] <= lag <= DEPARTURE_SEEN_WINDOW[1]


def delay_secs(arrived_at: datetime, service_date: date, start_secs: int, offset_secs: int) -> int:
    """Positive = late. Scheduled arrival = actual departure + timetable travel time."""
    return round(service_day_secs(arrived_at, service_date) - (start_secs + offset_secs))


def is_on_time(delay: int) -> bool:
    return -ON_TIME_EARLY_SECS <= delay <= ON_TIME_LATE_SECS


@dataclass(frozen=True)
class LiveEstimate:
    target: StopPoint  # the next visit to the stop we were asked about
    current_delay_secs: int | None  # at the last stop passed; None if unknown
    eta: datetime | None  # None if the departure wasn't seen (start_time untrustworthy)


def live_estimate(
    fixes: list[Fix],
    stops: list[StopPoint],
    stop_id: str,
    service_date: date | None,
    start_secs: int | None,
) -> LiveEstimate | None:
    """Where a live bus is relative to `stop_id`, or None if it has already passed it.

    ETA = actual departure + timetable travel time to the stop + the delay it's running
    at its last stop passed (assumes it neither recovers nor loses more time)."""
    arrivals = detect_arrivals(fixes, stops)
    passed = max((a.sequence for a in arrivals), default=-1)
    target = next((s for s in stops if s.stop_id == stop_id and s.sequence > passed), None)
    if target is None:
        return None
    seen = (
        bool(fixes)
        and service_date is not None
        and departure_seen(fixes[0].t, service_date, start_secs)
    )
    if not seen:
        return LiveEstimate(target, None, None)
    offsets = {s.sequence: s.offset_secs for s in stops}
    judged = [a for a in arrivals if offsets[a.sequence] > 0]  # first stop = departure itself
    current = (
        delay_secs(judged[-1].t, service_date, start_secs, offsets[judged[-1].sequence])
        if judged
        else None
    )
    eta = service_midnight(service_date) + timedelta(
        seconds=start_secs + target.offset_secs + (current or 0)
    )
    return LiveEstimate(target, current, eta)


# ---------- coverage ----------


def departures_per_hour(windows: Iterable[Window]) -> dict[int, int]:
    """Scheduled departures per service-day hour (can be >= 24 after midnight).
    A window departs at start, start + headway, ... while before end."""
    counts: dict[int, int] = defaultdict(int)
    for w in windows:
        for secs in range(w.start_secs, w.end_secs, w.headway_secs):
            counts[secs // 3600] += 1
    return dict(counts)
