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

## Phase 3

**Main measure: delay per bus against its own departure. Headways only where coverage is >= 80%.** (Approved by Howard 2026-11-06.)
Why: the feed shows only ~27-34% of scheduled runs, and a missing bus looks exactly like a long gap between buses. The live `start_time` is the run's actual departure (first fix a median 0.4 min later), so `delay = arrival - (departure + timetable travel time)` judges each bus on its own.
Rejected: headways for every route (mostly measures GPS coverage), and headways only (just ~11 routes qualify at midday).

**Delay only for runs whose departure we saw: first GPS fix within -1 to +3 min of `start_time`.** About half of runs qualify. The other 10% are first seen an hour or more after `start_time`, which gave p95 delays of 200+ min.
Rejected: "the run was detected at its first stop". Buses' GPS gets a median 137 m from the timetable's first-stop pin (terminal bays), so that filter dropped 90% of good runs.

**The first stop gets no delay.** Its scheduled offset is 0, so it is the departure itself and would always count as "on time".

**On time = between 1 min early and 5 min late.** A common transit definition. The rule is in Python (`is_on_time`) and stored per arrival, so SQL only averages it. The same applies to the headway rule (`is_regular`: MTA-style wait assessment, +3 min allowance for scheduled headways up to 10 min, +5 min above that).

**Stop arrival = the first moment the GPS track comes within 60 m of the stop, interpolated between fixes, following stop order.**
Why 60 m: measured on 366 real runs. 30/45/60/80/100/150 m gave median coverage of passed stops of 85/92/95/98/100/100%. Matched tracks pass a median 15 m from the stop (p90 46 m) at 60 m. Larger radii add little and risk matching parallel roads.
Known bias: "first within 60 m" is up to radius / speed early, about 7 s at 8 m/s.
Order matters: 956 trips are loops and 1,024 visit a stop twice. A stop at the same place as an earlier one can only match when it's the very next stop (a test caught the bug where it was matched while skipping ahead).
Rejected: "a fix within 75 m of the stop" (the brief's idea). Fixes are ~300 m apart, so most stops would never get a fix that close.

**Group headways by `(shape_id, stop_sequence, service_date)`, and look up scheduled headways by shape + actual departure time.**
Why: the trip ID's `_N` index matched the frequency window only 54% of the time, while shape + time matched 88%. Overlapping windows combine as 1/h = 1/h1 + 1/h2. Grouping by sequence keeps a loop's start and end apart. Unknown live trips borrow the stop list of a timetable trip with the same shape (`trip_id` always embeds the `shape_id`).

**Exclude anything our own outages could distort.** Headways spanning a gap of more than 180 s without a successful poll are set to NULL. Coverage hours that overlap an outage are flagged `ingestion_complete = false` and left out of the stats, so the PC sleeping or losing network doesn't look like missing buses. An hour with no complete coverage and no delay data gets no stats row at all, rather than a misleading "0% seen".

**One meaning of "hour" everywhere: the KL clock hour the bus left its first stop.** Delay, coverage and headways then describe the same set of buses.
Rejected: the arrival hour for delays (it would mix buses that left in different hours with coverage computed by departure).

**Recompute in full, per service day, in one transaction (today + yesterday every 15 min). Rebuild stats in full from the last 14 days.**
Why: simple and always consistent with the latest code and timetable. Re-running gives identical results (tested). About 1.4 s for 2.6 h of data. Has a `shortcut:` note for when it gets slow.
Rejected: incremental updates (extra state and edge cases for a job this cheap).

**`stop_arrivals` is kept indefinitely; raw positions are deleted after 14 days.** Arrivals are the product, about 3x fewer rows than the raw GPS.

**Stats percentiles in Postgres (`percentile_cont`), detection in Python.** SQL handles group-by aggregation well. The geometry and stateful matching are much clearer, and testable, in Python.

**Edited the unreleased Phase 3 migration instead of stacking a fix on it.** It was never committed or deployed, so I rolled it back locally and regenerated it. Committed migrations are never edited.

## Phase 4

**`/stops/{id}/next` returns two lists: `live` and `scheduled`.**
Why: the timetable only says "every N minutes", so a list of timetabled buses can only be an approximate grid. What a rider wants is "which buses are actually coming". `live` uses buses heard from in the last 5 min whose run still has this stop ahead. Their position is found by re-running the Phase 3 stop detection on their last 4 h of GPS. ETA = actual departure + travel time to this stop + current delay at the last stop passed. `scheduled` is the grid for the next 90 min, each entry with the typical (median) delay at *this stop* for that departure hour over 14 days, plus its sample size.
Rejected: only the timetable grid (looks precise, isn't), and only live buses (two thirds of buses are invisible in the feed).

**Every response time is in Kuala Lumpur time (`+08:00`).** A pydantic `PlainSerializer` on one `KLTime` type. Internally everything stays timezone-aware UTC. Found when a test showed some fields in UTC and others in +08:00.

**One error shape for every 4xx/5xx: `{"error": {"status", "message", "details"}}`.** Handlers for HTTP errors, validation errors (`details` lists each bad field and the problem) and database errors (503 "database unavailable", with no SQL or hostnames in the body; the real error goes to the log). `/health` keeps its own body because monitoring reads it.

**Validation at the edge:** `hour` 0-23, `day_type` weekday|weekend, `limit` 1-50, `offset` 0-1000, `min_buses` >= 1, IDs limited to `[A-Za-z0-9_-]{1,32}`. Defaults for `hour`/`day_type` come from the current KL time. Unknown route or stop → 404. A known route with no data for that hour → 200 with an empty list (not an error).

**`/routes/worst` ranks by on-time % (lowest first), then median delay, and hides route-hours with fewer than `min_buses` (default 5) bus runs.** The response includes `total` and echoes `limit`/`offset` for pagination.
Rejected: ranking by median delay alone. It hides routes that are often early, and early is also unreliable.

**In-process TTL cache: 60 s for route stats, 15 s for next buses.** Stats only change every 15 min. Errors are never cached (tested).
`shortcut:` each API process keeps its own cache; use Redis if several replicas must agree.
The timetable (~1-2 s to build) is cached per process and rebuilt only when a newer static load has finished.

**Index `stop_arrivals(stop_id, service_date)`**, measured 35.4 → 3.8 ms on a 606k-row table (see PERFORMANCE.md). No new index for `live`: it reads `vehicle_positions` by `observed_at` and `(vehicle_id, observed_at)`, both already indexed.

**Thin endpoints.** `app/api.py` validates and shapes responses, `app/queries.py` reads the database, and `app/schemas.py` defines the response models (and the OpenAPI docs). Each part can be tested and explained on its own.

## Phase 5

**GitHub Actions with three parallel jobs: lint, test, docker.**
- `test` runs against a real `postgres:16` service container, not SQLite, because the code relies on Postgres features (`DISTINCT ON`, `percentile_cont`, `NULLS NOT DISTINCT`, partial indexes).
- `REQUIRE_TEST_DB=1` makes database tests fail, not skip, if the database is missing, so CI can't pass with half the suite quietly skipped.
- Coverage threshold (80%) is in `pyproject.toml`, so local runs and CI agree. It currently measures ~95%.
- `docker` builds the image and smoke-tests it (imports every module, lists migrations).
- Same uv version as the Dockerfile; `uv sync --frozen` fails if `uv.lock` is out of date. `concurrency` cancels superseded runs, and `permissions: contents: read` is least privilege.
Rejected: pushing images to a registry (no deploy target yet), and a matrix of Python versions (we ship one image with 3.12).

**Coverage work went into real gaps, not the number.** The live-bus current-delay/ETA logic was untested inside a DB-heavy function, so it was pulled out as a pure function (`headways.live_estimate`) and tested directly. The JSON log formatter got tests (it's a claimed feature). Left untested on purpose: `gtfs_static.download()` (a once-a-day call with a 60 s sleep on 429) and the `main()` entry points.

**Postgres and the API bind to `127.0.0.1` by default.** Found while writing the deploy guide: Compose was publishing Postgres on all interfaces with the default password. Fine on a laptop, dangerous on a VM. Local use is unchanged. On a server the API goes behind an HTTPS reverse proxy (Caddy).

**Deploy = the same Docker Compose stack on one Linux VM.** The one-shot `migrate` service already runs migrations on every `up`, and the worker and API wait for it to succeed.
Recommended host: Oracle Cloud Always Free (ARM, €0), converted to Pay As You Go to avoid idle-instance reclaim, with a budget alert. Alternative: a ~€5/month VPS.
Rejected: Render free (spins down, no free worker, Postgres expires). **Not set up: needs Howard's decision and card.**

**Database URLs use `127.0.0.1`, not `localhost`.** Found right after binding Postgres to IPv4 loopback only: on Windows, `localhost` resolves to IPv6 `::1` first, and connecting there *hung* (a single DB test went from 0.9 s to a 120 s timeout) instead of falling back to IPv4. Measured both ways before changing every URL.
