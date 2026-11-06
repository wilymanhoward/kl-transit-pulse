"""Build GTFS-realtime protobuf bytes for tests."""

from datetime import UTC, datetime
from pathlib import Path

from google.transit import gtfs_realtime_pb2

RECORDED = Path(__file__).parent / "fixtures" / "vehicle_positions_20261105.pb"
RECORDED_FEED_TS = datetime(2026, 11, 5, 10, 27, 1, tzinfo=UTC)  # its header timestamp


def make_feed(feed_ts: datetime, vehicles: list[dict]) -> bytes:
    """vehicles: dicts with optional keys id, plate, lat, lon, ts, trip_id, route_id,
    start_time, start_date, bearing, speed. Omitted keys stay unset in the protobuf."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = int(feed_ts.timestamp())
    for i, v in enumerate(vehicles):
        e = feed.entity.add()
        e.id = str(i)
        vp = e.vehicle
        if "id" in v:
            vp.vehicle.id = v["id"]
        if "plate" in v:
            vp.vehicle.license_plate = v["plate"]
        if "lat" in v:
            vp.position.latitude = v["lat"]
            vp.position.longitude = v["lon"]
        for key in ("bearing", "speed"):
            if key in v:
                setattr(vp.position, key, v[key])
        if "ts" in v:
            vp.timestamp = int(v["ts"].timestamp())
        for key in ("trip_id", "route_id", "start_time", "start_date"):
            if key in v:
                setattr(vp.trip, key, v[key])
    return feed.SerializeToString()


def bus(vehicle_id: str, ts: datetime, **extra: object) -> dict:
    return {"id": vehicle_id, "lat": 3.1, "lon": 101.6, "ts": ts, "trip_id": "T1", **extra}
