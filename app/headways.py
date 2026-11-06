"""Pure headway estimation: GPS fixes + stop list in, stop arrivals and headways out.

No I/O here, so every edge case is unit-testable. app/estimate.py does the database side.

These are ESTIMATES from GPS. Rapid KL timetables are frequency-based ("every 20 min"),
so we don't measure "minutes late"; we measure the gap between consecutive buses at a
stop (the headway) and compare it with the scheduled gap.

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
from datetime import datetime
from itertools import pairwise

EARTH_RADIUS_M = 6_371_000


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
