# Decisions

Short log: decision, why, alternative rejected.

## Phase 0

**Use `uv` for the virtualenv and dependencies.**
Why: already installed, one fast command (`uv sync`) creates the venv from `pyproject.toml`, and it writes a lock file so CI gets the same versions.
Rejected: plain `pip` + `requirements.txt` (no lock file by default, slower).

**Only add dependencies when a phase needs them.**
Why: Phase 0 only needs `httpx` and `gtfs-realtime-bindings`. FastAPI, SQLAlchemy and the rest come in later phases, so every dependency has a reason I can explain.
Rejected: installing the whole stack up front.

**Don't commit live samples (`data/samples/` is gitignored).**
Why: the static ZIP is 1.7 MB, it changes daily and anyone can re-download it. Tests will use small fixtures I build by hand in `tests/fixtures/`.
Rejected: committing real downloads.

**The inspector discovers populated fields by walking the protobuf (`ListFields`), not from a hard-coded list.**
Why: the point of Phase 0 is to find out what is actually there, without assuming.
Rejected: checking a fixed list of expected fields.

**Rate limit in the fetch script: exactly one request per API, one retry after 60 s on 429.**
Why: 1 request per API is far under 4/min. Real backoff belongs to the Phase 2 ingester.
Rejected: building full retry/backoff logic into a one-off script.

**Measure headway adherence instead of timetable delay.** (Approved by Howard 2026-11-05, see DATA_FINDINGS.md.)
Why: `frequencies.txt` uses `exact_times=0` for 99.9% of trips, so there are no scheduled times to compare against.
Rejected: inventing scheduled times from the headway grid (only 2/88 live trips line up with it).

## Phase 1

**Store GTFS times as integer seconds after midnight of the service day (`arrival_secs`).**
Why: GTFS allows hours above 23 (the real feed goes to 25:15). Postgres `TIME` stops at 24:00. Integers also make "difference in seconds" trivial for headway maths.
Rejected: `TIME` (can't hold 25:15), `INTERVAL` (works, but harder to explain and to index).

**Reload the static timetable by deleting and re-inserting all static rows in one transaction.**
Why: the feed is a full snapshot, so trips removed upstream must disappear too. One transaction means readers never see a half-loaded timetable, and a failed load rolls back to the old one (tested). It takes about 4 s for 97k rows. I use `DELETE`, not `TRUNCATE`, because `TRUNCATE` locks out readers.
Rejected: per-row upsert (`INSERT ... ON CONFLICT`), which needs extra logic to remove rows that vanished.

**No foreign keys from `vehicle_positions` to `trips` or `routes`.**
Why: 15% of live trip IDs aren't in the static feed, and static rows are replaced daily. A foreign key would reject real positions or block the reload.
Rejected: foreign keys plus a placeholder "unknown trip" row.

**Added `frequencies` and `calendar` tables (not in the brief).**
Why: headway adherence needs the scheduled headway (`frequencies`), and weekday vs weekend needs `calendar`. Skipped `shapes.txt` (69k points) and `agency.txt` until Phase 3 shows a need.

**Indexes, each tied to a query we'll run:**
| Index | Query it serves |
|---|---|
| `trips(route_id)` | trips of a route (reliability, worst routes) |
| `stop_times` PK `(trip_id, stop_sequence)` | stops of a trip in order (arrival detection) |
| `stop_times(stop_id)` | trips serving a stop (`/stops/{id}/next`) |
| `frequencies` PK `(trip_id, start_secs)` | headway for a trip at a time |
| `vehicle_positions` unique `(vehicle_id, observed_at)` | drops duplicate fixes seen in two snapshots; also a vehicle's trajectory in time order |
| `vehicle_positions(observed_at)` | retention delete and `/health` "rows in last hour" |
| partial `ingestion_runs(kind, finished_at) WHERE status='success'` | `/health` "last successful run" |

I deliberately did not add a route+time index on `vehicle_positions` yet. Phase 3 adds it with a before/after `EXPLAIN ANALYZE`, as the brief asks.

**Identify one bus run as `(trip_id, trip_start_date, trip_start_secs)`.**
Why: with frequency-based service, one `trip_id` is a template reused many times a day. The live `start_time` tells the runs apart.

**Trailing slash on API URLs.**
Why: `/prasarana?...` answers with a 301 to `/prasarana/?...`, which doubles the requests against a 4/min limit.

**Test DB via a Postgres init script, DB tests skipped without `TEST_DATABASE_URL`.**
Why: the parser tests run anywhere. The loader tests need real Postgres because they check foreign keys, transactions and the migration itself. Every DB test runs `alembic downgrade base` then `upgrade head`, so both directions of the migration are tested.
Rejected: SQLite for tests, because it behaves differently from Postgres.

**Fixture feed kept as plain `.txt` files and zipped at test time.**
Why: readable and shows up in diffs. The brief said "fixture ZIP"; this is the same data in a reviewable form.

## Phase 2

**A plain loop in its own worker process, not APScheduler.**
Why: one job per tick, run in order. That makes the rate limit easy to prove: one realtime request per tick, ticks start at least `poll_seconds` apart, and `Settings` refuses anything under 15 s (60/4), so the worker can't exceed 4 req/min. It also means no extra dependency, and the API process stays separate (it can restart without stopping ingestion).
Rejected: APScheduler (overlapping-job and missed-run settings to explain, with no benefit for 3 jobs). Also rejected: a token-bucket rate limiter (not needed when one request per tick is guaranteed).

**Backoff between ticks, not retries inside a tick.**
Why: after n consecutive failures, wait `30 s × 2^n`, capped at 5 min, and never less than the server's `Retry-After`. Any exception counts: 429, 5xx, timeout, corrupt protobuf, database down. Retries inside a tick would multiply requests per tick and break the "one request per tick" proof.
Rejected: random jitter (it matters when many clients retry at once; there's one client here).

**Two layers of de-duplication.**
1. Snapshot level: skip a feed whose header timestamp is ≤ the last processed one, or older than 5 min (`status='skipped'`). The last timestamp is read from `ingestion_runs.feed_timestamp`, so this survives a restart.
2. Row level: `INSERT ... ON CONFLICT (vehicle_id, observed_at) DO NOTHING`. A new snapshot often repeats a bus whose GPS hasn't updated.

**Store the vehicle's own GPS `timestamp` as `observed_at`, not the fetch time.**
Why: positions are 30 to 100 s old when we fetch them. Fall back to the header time only if a vehicle has none.

**Reject entities without a vehicle id or a valid position, and treat (0, 0) as "no GPS fix".**
Why: the feed comes from outside, so validate at the boundary. A row we can't de-duplicate or place on a map is useless. Bad `start_time` or `start_date` values become NULL, and the row is kept.

**`/health` checks both ingestion runs and the newest GPS fix, and returns 503 when unhealthy.**
Why: a run can "succeed" while the upstream feed is frozen. Only the age of the newest position catches that. 503 lets Docker or a hosting platform alert or restart on it.

**The daily timetable refresh lives in the worker (after 04:00 Kuala Lumpur time).**
Why: it's one more job in the same loop, and it loads the timetable on first start too. After a failure it retries every 15 min, not every tick.

**One-shot `migrate` service in Compose.**
Why: `worker` and `api` both wait for `service_completed_successfully`, so exactly one process runs migrations and nothing starts against a half-migrated schema.

**JSON-lines logs with standard `logging` and a 20-line formatter.**
Why: fields such as `run_id`, `inserted` and `feed_age_s` can be searched without parsing free text. httpx's per-request INFO log is turned down because it adds 2,880 lines a day.
Rejected: `structlog` (a dependency for a 20-line job).

**Not pruning `ingestion_runs` yet.**
Why: about 2,900 rows a day is tiny, and it's useful uptime history. Positions reference runs, so pruning needs care. Revisit if it ever matters.
