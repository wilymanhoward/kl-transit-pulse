# KL Transit Pulse

Measures how reliable Kuala Lumpur's Rapid KL buses really are, using live GPS positions and the published timetable from Malaysia's open data portal.

> Status: Phase 3 (reliability estimation). See [docs/DATA_FINDINGS.md](docs/DATA_FINDINGS.md) for what the real data looks like.

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
| `api` | `http://localhost:8000/health` (and `/docs` for the OpenAPI page) |

```bash
curl localhost:8000/health        # 200 when data is fresh, 503 when stale
docker compose logs -f worker     # JSON log lines
```

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
