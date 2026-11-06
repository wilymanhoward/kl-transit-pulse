# KL Transit Pulse

Measures how reliable Kuala Lumpur's Rapid KL buses really are, using live GPS positions and the published timetable from Malaysia's open data portal.

> Status: Phase 4 (API). See [docs/DATA_FINDINGS.md](docs/DATA_FINDINGS.md) for what the real data looks like.

Rapid KL bus timetables are frequency-based ("every 15 min"), not fixed times, and the live feed only carries about a third of scheduled buses. So the main measure is **delay per bus against its own departure**: the feed reports when each run actually left, and the timetable gives the travel time to every stop. Gaps between buses (headways) are reported only for route-hours where the feed shows at least 80% of scheduled runs. Every figure is an **estimate from GPS** and comes with its sample size and coverage.

## Run it

Needs Docker Desktop.

```bash
docker compose up --build -d
```

This starts:

| Service | What it does |
|---|---|
| `db` | PostgreSQL 16 on `localhost:5432` |
| `migrate` | applies Alembic migrations, then exits |
| `worker` | polls live bus positions every 30 s, re-estimates arrivals, delays and route stats every 15 min, reloads the timetable daily after 04:00 KL time, deletes positions older than 14 days |
| `api` | the HTTP API on `http://localhost:8000` (interactive docs at `/docs`) |

```bash
docker compose logs -f worker     # JSON log lines
```

## API

All times are Kuala Lumpur time (`+08:00`). Every figure is an estimate and comes with its sample size. Every error looks like `{"error": {"status": 404, "message": "...", "details": null}}`.

| Endpoint | What it returns |
|---|---|
| `GET /health` | 200 when data is fresh, 503 when ingestion stopped or the feed froze |
| `GET /routes/{route_id}/reliability?hour=&day_type=` | per departure hour: coverage, on-time %, median/p90 delay, headway stats (only where coverage >= 80%) |
| `GET /routes/worst?hour=&day_type=&limit=&offset=&min_buses=` | least reliable routes for an hour (default: now), hiding route-hours with fewer than `min_buses` runs |
| `GET /stops/{stop_id}/next?limit=` | `live`: buses on their way, with ETA and current delay. `scheduled`: the next 90 min of the timetable grid with typical delays at this stop |

Real examples (2026-11-06, after 3 h of data):

```bash
curl "localhost:8000/routes/worst?hour=12&min_buses=3&limit=3"
```

```json
{"day_type": "weekday", "hour": 12, "min_buses": 3, "total": 6, "limit": 3, "offset": 0,
 "items": [{"rank": 1, "route": {"route_id": "U7508", "short_name": "750",
            "long_name": "Hab Sekysen 2 ~ Hab Pasar Seni"},
            "on_time_pct": 11.25, "median_delay_secs": 1254.5, "p90_delay_secs": 1999.3,
            "delay_sample_size": 80, "delay_buses": 3, "coverage_pct": null}, "..."],
 "notes": ["Estimates from GPS positions ...", "..."]}
```

```bash
curl "localhost:8000/stops/1001512/next?limit=3"     # KL Sentral
```

```json
{"stop": {"stop_id": "1001512", "name": "KL1079 KL SENTRAL", "lat": 3.132961, "lon": 101.687618},
 "live": [],
 "scheduled": [{"route": {"route_id": "U8210", "short_name": "821", "...": "..."},
                "scheduled_arrival": "2026-11-06T16:13:43+08:00", "scheduled_headway_secs": 660,
                "typical_delay_secs": 466.0, "typical_delay_sample_size": 1}, "..."]}
```

(`live` was empty because the PC had lost its internet connection; `coverage_pct` is null until the worker has collected uninterrupted hours.)

Settings (see `.env.example`): `POLL_SECONDS` (default 30, minimum 15 to respect the 4 req/min limit), `RETENTION_DAYS` (14), `STALE_AFTER_SECONDS` (300).

## Develop

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
docker compose up -d db
uv run alembic upgrade head
uv run python -m app.worker          # or: uv run uvicorn app.api:app --reload
uv run python -m app.estimate --days 2   # re-estimate the last 2 service days now
```

Performance notes and the EXPLAIN ANALYZE comparison: [docs/PERFORMANCE.md](docs/PERFORMANCE.md).

Tests (DB tests need the compose database and are skipped without `TEST_DATABASE_URL`):

```bash
TEST_DATABASE_URL=postgresql+psycopg://transit:transit@localhost:5432/transit_test uv run pytest
```

On Windows PowerShell, set the variable first: `$env:TEST_DATABASE_URL="postgresql+psycopg://transit:transit@localhost:5432/transit_test"`, then `uv run pytest`.

Lint: `uv run ruff check . && uv run ruff format --check .`

Data reconnaissance scripts: `uv run python scripts/fetch_samples.py` then `uv run python scripts/inspect_samples.py`.

## Data source and licence

Data from [data.gov.my](https://developer.data.gov.my) (GTFS Static and GTFS Realtime for Prasarana `rapid-bus-kl`), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Credit: **data.gov.my**.

## Limitations

- Reliability figures will be **estimates** derived from GPS positions, not official figures.
- The API allows 4 requests per minute.
- Rapid Rail (LRT/MRT) has no stable realtime feed, so it is out of scope.
- The timetable has no public-holiday exceptions.
