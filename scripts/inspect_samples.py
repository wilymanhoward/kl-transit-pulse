"""Print facts about the downloaded samples so we design from real data, not assumptions.

Usage: uv run python scripts/inspect_samples.py
"""

import csv
import io
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from google.protobuf.message import Message
from google.transit import gtfs_realtime_pb2

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


def populated_paths(msg: Message, prefix: str = "") -> set[str]:
    """Dotted names of every field actually set in a protobuf message (recursive)."""
    paths: set[str] = set()
    for field, value in msg.ListFields():
        name = prefix + field.name
        paths.add(name)
        if isinstance(value, Message):  # nested message (repeated fields come back as lists)
            paths |= populated_paths(value, name + ".")
    return paths


def read_csv(zf: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    with zf.open(name) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))


def to_seconds(hms: str) -> int:
    h, m, s = (int(x) for x in hms.strip().split(":"))
    return h * 3600 + m * 60 + s


def inspect_realtime() -> list[gtfs_realtime_pb2.FeedEntity]:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString((SAMPLES / "vehicle_positions.pb").read_bytes())
    fetched_at = datetime.fromisoformat((SAMPLES / "fetched_at.txt").read_text().strip())

    print("=== REALTIME (vehicle positions) ===")
    print(f"header: {sorted(populated_paths(feed.header))}")
    header_ts = datetime.fromtimestamp(feed.header.timestamp, UTC)
    print(f"header.timestamp: {header_ts.isoformat()} (fetched {fetched_at.isoformat()})")

    vehicles = [e for e in feed.entity if e.HasField("vehicle")]
    print(f"entities: {len(feed.entity)}, with vehicle: {len(vehicles)}")
    if not vehicles:
        return []

    counts: Counter[str] = Counter()
    for e in vehicles:
        counts.update(populated_paths(e.vehicle))
    print("field population (fraction of vehicles):")
    for path, n in sorted(counts.items()):
        print(f"  {path:40s} {n / len(vehicles):6.1%}")

    ages = sorted(
        (fetched_at - datetime.fromtimestamp(e.vehicle.timestamp, UTC)).total_seconds()
        for e in vehicles
        if e.vehicle.timestamp
    )
    if ages:
        print(
            f"vehicle timestamp age vs fetch (s): min {ages[0]:.0f}, "
            f"median {ages[len(ages) // 2]:.0f}, max {ages[-1]:.0f}"
        )
        print(f"  older than 5 min: {sum(a > 300 for a in ages)}/{len(ages)}")

    print("first 3 entities raw:")
    for e in vehicles[:3]:
        print("  ---\n  " + str(e).replace("\n", "\n  "))
    return vehicles


def inspect_static(vehicles: list[gtfs_realtime_pb2.FeedEntity]) -> None:
    print("\n=== STATIC GTFS ===")
    with zipfile.ZipFile(SAMPLES / "gtfs_static.zip") as zf:
        names = zf.namelist()
        print(f"files: {names}")
        routes = read_csv(zf, "routes.txt")
        stops = read_csv(zf, "stops.txt")
        trips = read_csv(zf, "trips.txt")
        stop_times = read_csv(zf, "stop_times.txt")
        frequencies = read_csv(zf, "frequencies.txt") if "frequencies.txt" in names else []
        for extra in ("calendar.txt", "calendar_dates.txt", "agency.txt"):
            if extra in names:
                rows = read_csv(zf, extra)
                print(f"{extra}: {len(rows)} rows, columns {list(rows[0]) if rows else []}")
                for r in rows[:3]:
                    print(f"  {r}")

    print(
        f"routes: {len(routes)}, stops: {len(stops)}, trips: {len(trips)}, "
        f"stop_times: {len(stop_times)}"
    )
    print(f"trips.txt columns: {list(trips[0])}")
    print(f"stop_times.txt columns: {list(stop_times[0])}")
    print(f"sample static trip_ids: {[t['trip_id'] for t in trips[:3]]}")
    print(f"sample static route_ids: {[r['route_id'] for r in routes[:5]]}")

    secs = [to_seconds(st["arrival_time"]) for st in stop_times if st.get("arrival_time")]
    over_24 = sum(s >= 24 * 3600 for s in secs)
    print(
        f"stop_times arrival_time: max {max(secs) // 3600}:{max(secs) % 3600 // 60:02d}, "
        f"rows >= 24:00:00: {over_24}"
    )
    trips_with_times = {st["trip_id"] for st in stop_times}
    missing = sum(t["trip_id"] not in trips_with_times for t in trips)
    print(f"trips with no stop_times rows: {missing}/{len(trips)} ({missing / len(trips):.1%})")

    first_times = Counter(
        st["arrival_time"] for st in stop_times if st["stop_sequence"] in ("0", "1")
    )
    print(f"most common first-stop arrival_time: {first_times.most_common(3)}")

    if frequencies:
        print(f"\nfrequencies.txt: {len(frequencies)} rows, columns {list(frequencies[0])}")
        for r in frequencies[:3]:
            print(f"  {r}")
        freq_trips = {f["trip_id"] for f in frequencies}
        print(f"trips covered by frequencies: {len(freq_trips)}/{len(trips)}")
        print(f"exact_times values: {Counter(f.get('exact_times', '') for f in frequencies)}")
        headways = sorted(int(f["headway_secs"]) for f in frequencies)
        print(
            f"headway_secs: min {headways[0]}, median {headways[len(headways) // 2]}, "
            f"max {headways[-1]}"
        )

    if not vehicles:
        return
    unmatched = sorted({e.vehicle.trip.trip_id for e in vehicles} - {t["trip_id"] for t in trips})
    print(f"\nlive trip_ids NOT in trips.txt: {unmatched}")
    if frequencies:
        # Does the live start_time line up with a frequency window for that trip?
        windows: dict[str, list[tuple[int, int, int]]] = {}
        for f in frequencies:
            windows.setdefault(f["trip_id"], []).append(
                (to_seconds(f["start_time"]), to_seconds(f["end_time"]), int(f["headway_secs"]))
            )
        inside = on_grid = 0
        for e in vehicles:
            t = e.vehicle.trip
            start = to_seconds(t.start_time)
            for lo, hi, step in windows.get(t.trip_id, []):
                if lo <= start <= hi:
                    inside += 1
                    on_grid += (start - lo) % step == 0
                    break
        print(
            f"live start_time inside a frequency window: {inside}/{len(vehicles)}, "
            f"exactly on the headway grid: {on_grid}"
        )
    static_trips = {t["trip_id"] for t in trips}
    static_routes = {r["route_id"] for r in routes}
    live_trips = [e.vehicle.trip.trip_id for e in vehicles if e.vehicle.trip.trip_id]
    live_routes = [e.vehicle.trip.route_id for e in vehicles if e.vehicle.trip.route_id]
    print("\n=== LIVE vs STATIC ===")
    print(f"sample live trip_ids: {live_trips[:5]}")
    if live_trips:
        hit = sum(t in static_trips for t in live_trips)
        print(
            f"live trip_ids found in trips.txt: {hit}/{len(live_trips)} "
            f"({hit / len(live_trips):.1%})"
        )
        with_times = sum(t in trips_with_times for t in live_trips)
        print(f"live trip_ids with stop_times rows: {with_times}/{len(live_trips)}")
    if live_routes:
        hit = sum(r in static_routes for r in live_routes)
        print(
            f"live route_ids found in routes.txt: {hit}/{len(live_routes)} "
            f"({hit / len(live_routes):.1%})"
        )
        print(f"distinct live routes: {len(set(live_routes))}")


if __name__ == "__main__":
    inspect_static(inspect_realtime())
