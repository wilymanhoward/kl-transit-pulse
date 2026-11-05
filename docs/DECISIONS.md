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
