# KL Transit Pulse

Measures how reliable Kuala Lumpur's Rapid KL buses really are, using live GPS positions and the published timetable from Malaysia's open data portal.

> Status: Phase 1 (database + static timetable). See [docs/DATA_FINDINGS.md](docs/DATA_FINDINGS.md) for what the real data looks like.

Rapid KL bus timetables are frequency-based ("every 15 min"), not fixed times. So reliability is measured as **headway adherence**, meaning how evenly buses actually arrive compared with the scheduled gap, rather than "minutes late".

## Run it

Needs Docker Desktop.

```bash
docker compose up --build -d
```

This starts PostgreSQL 16, applies the migrations and loads the current timetable, then the `app` container exits (`docker compose logs app` shows the load). The database stays up on `localhost:5432`.

## Develop

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
docker compose up -d db
uv run alembic upgrade head
uv run python -m app.gtfs_static
```

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
