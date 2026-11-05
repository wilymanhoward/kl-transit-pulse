"""Download one realtime snapshot and the static GTFS ZIP into data/samples/.

Makes exactly one request to each API (rate limit is 4 req/min per API).
On HTTP 429 it waits 60 s and retries once, so a re-run never hammers the API.

Usage: uv run python scripts/fetch_samples.py
"""

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

BASE = os.getenv("GTFS_API_BASE", "https://api.data.gov.my")
CATEGORY = os.getenv("GTFS_CATEGORY", "rapid-bus-kl")
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "samples"

TARGETS = {
    "vehicle_positions.pb": f"{BASE}/gtfs-realtime/vehicle-position/prasarana",
    "gtfs_static.zip": f"{BASE}/gtfs-static/prasarana",
}


def fetch(client: httpx.Client, url: str) -> bytes:
    for attempt in range(2):
        resp = client.get(url, params={"category": CATEGORY})
        if resp.status_code == 429 and attempt == 0:
            print("  429 rate limited, waiting 60 s before one retry...")
            time.sleep(60)
            continue
        resp.raise_for_status()
        return resp.content
    raise RuntimeError(f"still rate limited after retry: {url}")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for filename, url in TARGETS.items():
            print(f"GET {url}?category={CATEGORY}")
            data = fetch(client, url)
            path = OUT_DIR / filename
            path.write_bytes(data)
            print(f"  saved {path.name}: {len(data):,} bytes")
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    (OUT_DIR / "fetched_at.txt").write_text(stamp + "\n")
    print(f"done at {stamp}")


if __name__ == "__main__":
    main()
