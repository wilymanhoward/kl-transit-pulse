# Interview notes

## Phase 0: reconnaissance

**What it is, in plain English.** Before designing anything I downloaded one real snapshot of the live bus feed and the timetable, and wrote a script that measures what is really in them: which fields are filled in, how fresh the data is, and whether the live buses can be linked to the timetable. The results are in `docs/DATA_FINDINGS.md`.

**Key concepts.**
- **GTFS Static**: a ZIP of CSV files describing routes, stops and timetables.
- **GTFS Realtime**: a Protocol Buffers (protobuf, a compact binary format from Google) message, refreshed every 30 s. Here it only has vehicle positions.
- **Frequency-based service**: the timetable says "every N minutes" rather than fixed times (`frequencies.txt`, `exact_times=0`).

**Likely questions.**

1. *Why didn't you start by writing the delay calculation?*
   Because I didn't know what the data contained. Inspecting it first showed that Rapid KL buses run on headways, with no fixed times, so "minutes late" isn't defined. If I had built it first, the core logic would have been wrong.

2. *How did you handle the 4-requests-per-minute rate limit here?*
   The sample script makes exactly one request per API. On HTTP 429 it waits 60 s and retries once, then fails clearly. Proper exponential backoff comes in the ingester (Phase 2), which runs all the time.

3. *85% of live trips match the timetable. What do you do with the other 15%?*
   Every live route ID matches, so those buses still count in route-level statistics. Trip-level logic treats a missing trip as "unknown" instead of crashing, and I log the rate so I'd notice if it got worse.

## Phase 1: database and static timetable

**What it is, in plain English.** A PostgreSQL schema (created by Alembic migrations) for the timetable and for live GPS data, plus a loader that downloads the timetable ZIP, parses it and replaces the old timetable in one transaction. `docker compose up` starts Postgres, runs the migrations and loads the live timetable.

**Key concepts.**
- **Migration**: a versioned script that changes the schema, so every database (laptop, CI, production) ends up identical. `alembic upgrade head` applies them all.
- **Transaction**: a group of changes that either all happen or none do. The loader relies on this, so a broken feed can't leave the database half-empty.
- **Index**: a sorted lookup structure, like a book index. It speeds up reads but costs write time and space, so each one has to earn its place.

**Likely questions.**

1. *Why store times as seconds instead of a TIME column?*
   GTFS times can go past 24:00. A bus leaving at 23:50 is still part of "today's" service at 25:10. Postgres TIME can't store 25:10, and integer seconds also make subtracting times easy.

2. *What happens if the timetable download is corrupt halfway through loading?*
   The delete and all the inserts run in one transaction. Any error (for example a foreign-key violation) rolls everything back, so the old timetable stays, and the attempt is recorded as `failed` in `ingestion_runs`. A test checks exactly this.

3. *Why doesn't vehicle_positions have a foreign key to trips?*
   About 15% of live buses report trip IDs that aren't in the timetable, and the timetable is replaced daily. A foreign key would throw away real observations or block the reload. I keep the raw IDs and join when analysing.

## Phase 2: live ingestion

**What it is, in plain English.** A worker process wakes up every 30 seconds, downloads where every bus is, and saves the new positions. If the download fails, it waits longer each time (60 s, 120 s, 240 s, capped at 5 min) instead of hammering the server, and it never crashes. Once a day it reloads the timetable. Every hour it deletes positions older than 14 days. `GET /health` reports whether data is still arriving.

**Key concepts.**
- **Exponential backoff**: after each consecutive failure, double the wait. This gives a struggling server room to recover and keeps us under the rate limit.
- **Idempotent insert**: `ON CONFLICT DO NOTHING` means inserting the same GPS fix twice has no effect, so repeats in the feed are harmless.
- **Liveness vs freshness**: the worker can be "running fine" while the feed it reads is frozen. Health checks the data's age, not just the process.

**Likely questions.**

1. *How do you guarantee you never exceed 4 requests per minute?*
   Each tick makes exactly one realtime request, and the next tick starts at least `poll_seconds` after the previous one began. The config refuses values below 15 s, which is 60/4. Retries don't happen inside a tick, so a failure can only make the gap longer. A test runs the loop with mocked HTTP (429, 503, timeout) and checks the exact waits.

2. *What happens if the database goes down?*
   Every job in the loop is wrapped in its own try/except, so the error is logged and the loop backs off. When Postgres comes back, the next tick just works. There's a test that points the worker at a dead database and checks that it keeps looping with growing delays. `restart: unless-stopped` in Compose covers a real crash.

3. *Why did you store the bus's own timestamp instead of the time you fetched it?*
   The feed is 30 to 100 s old when we receive it, and buses report at different moments. Reliability maths needs to know when the bus was actually there. The vehicle timestamp is also part of the de-duplication key: same bus + same GPS time = same fix.

## Phase 3: reliability estimation

**What it is, in plain English.** For every bus run, I follow its GPS dots along its route's stop list and estimate when it passed each stop, interpolating between dots because they're ~300 m apart. Its delay at each stop is compared with when the timetable says it should get there *given when it actually left*. I also measure gaps between buses, but only for routes where the feed shows almost every scheduled bus, because the feed only carries about a third of them. Results are stored per stop arrival and summarised per route and hour: on-time %, median and p90 delay, how many buses the feed showed, and sample sizes.

**Key concepts.**
- **Headway**: the gap between consecutive buses. For "every 15 min" service it's what passengers feel.
- **Coverage / survivorship**: you can only measure what you see. Missing GPS looks like missing buses, so every statistic carries how much of the route was visible.
- **Interpolation**: estimate a value between two measurements, assuming steady movement between them.
- **Percentiles in SQL**: `percentile_cont(0.9) WITHIN GROUP (ORDER BY ...)` computes the p90 per group inside Postgres.

**Likely questions.**

1. *Why don't you just compare GPS time with the timetable, like "the 6:05 bus was 3 min late"?*
   Rapid KL publishes headways ("every 20 min"), not fixed times, so there is no "6:05 bus". The feed does give each run's real departure time, and the timetable gives travel time between stops. So I measure delay relative to the bus's own departure: if it left at 6:07 and the timetable allows 12 min to stop X, it's due at 6:19.

2. *How do you know your numbers mean anything?*
   I measured instead of assuming. The detector finds a median 95% of the stops a bus passes, and GPS tracks pass a median 15 m from the stops they match. When I found the feed shows only ~27-34% of scheduled buses, I stopped and changed the main metric, because headways would have mostly measured missing GPS. Every statistic carries its sample size and coverage, and our own outages are excluded. I also say clearly that these are estimates: arrival times are biased a few seconds early, and "on time" is measured from the departure, not from a published time.

3. *Loop routes start and end at the same stop. How do you avoid matching the end when the bus is just starting?*
   Matching only moves forward through the stop list, and a stop at the same place as an earlier stop can only match when it's the very next one expected. So the final stop can't be reached by skipping ahead. A unit test caught exactly this bug before I added the rule.

## Phase 4: API

**What it is, in plain English.** A FastAPI service with three endpoints. *Reliability* for one route, hour by hour. The *worst routes* for an hour, ignoring routes we barely saw. And *next buses at a stop*: buses actually on their way (from live GPS) plus the timetable for the next 90 minutes with typical delays. Inputs are validated, every error has the same JSON shape, hot responses are cached for a short time, and `/docs` shows interactive documentation generated from the code.

**Key concepts.**
- **Validation at the boundary**: check every input where it enters (here, FastAPI `Query`/`Path` constraints) so the rest of the code can trust it.
- **Pagination**: `limit` + `offset` + `total`, so a client can page through results without the server sending everything.
- **TTL cache**: keep a computed answer for N seconds; trade a little freshness for a lot less database work.
- **Serialisation**: converting Python objects to JSON. One place decides how times look (KL time, ISO 8601).

**Likely questions.**

1. *What happens if someone sends `hour=25` or a route ID with SQL in it?*
   FastAPI rejects it before my code runs: 422 with `{"error": {"message": "invalid request", "details": [{"field": "hour", ...}]}}`. Route and stop IDs must match `[A-Za-z0-9_-]{1,32}`, and queries are parameterised by SQLAlchemy anyway, so input is never pasted into SQL. Tests cover 11 bad-input cases.

2. *Why cache, and how do you stop it serving wrong data?*
   The reliability stats are only recomputed every 15 minutes, so a 60-second cache can't be meaningfully stale, and it cut repeated requests from ~7 ms to ~2 ms. Errors aren't cached, the cache is bounded in size, and the timetable cache reloads when a new timetable has been loaded. The limitation: it's per process, so with several replicas I'd move it to Redis.

3. *How does "next bus" work when the timetable has no fixed times?*
   Two lists. *Live*: buses the feed shows right now, placed on their route by the same stop-matching code as the estimator. Their ETA is when they left, plus the timetable's travel time, plus how late they're running. *Scheduled*: the "every N minutes" grid, clearly marked approximate, with the median delay we've measured at that stop for that hour and the sample size. I don't pretend the grid is a precise timetable.

## Phase 5: CI and deployment

**What it is, in plain English.** Every push to GitHub automatically runs the linter, runs all the tests against a real PostgreSQL database and measures coverage, and builds the Docker image. If any of those fail, the change is marked red. Deployment is the same `docker compose up` on a Linux server. Migrations run automatically first, and nothing starts if they fail.

**Key concepts.**
- **CI (continuous integration)**: an automated check on every change, so broken code is caught before it's merged.
- **Service container**: CI starts a throwaway Postgres next to the tests, so the tests use the real database engine.
- **Coverage**: which lines the tests executed. Useful for finding untested logic, misleading as a target on its own.
- **Least exposure**: bind internal services to localhost and put one HTTPS entry point in front.

**Likely questions.**

1. *Your coverage is 95%. Does that mean the code is correct?*
   No. It means 95% of lines ran during tests. What I care about is whether the risky logic is tested with meaningful cases: the stop matching has loop, jitter, missed-stop and past-midnight tests, and the API has bad-input tests. I used coverage to find a real gap, the live-bus delay estimate, and fixed it by extracting a pure function. I left a once-a-day download function untested on purpose rather than writing a hollow test.

2. *How do you stop CI from passing when the database tests didn't actually run?*
   Locally, the DB tests skip if there's no test database, which is convenient. In CI I set `REQUIRE_TEST_DB=1`, which turns that skip into a failure. I checked it: without the database URL, the 24 DB tests error instead of skipping.

3. *What did you find when preparing to deploy?*
   That my Compose file published Postgres on every network interface with a default password. Harmless on my laptop, a real hole on a public VM. I bound Postgres and the API to localhost, documented putting Caddy (HTTPS) in front, and said in the guide to never open port 5432. I also found that Oracle's free VMs get reclaimed if they look idle, which a low-CPU collector does, so the guide recommends converting to Pay As You Go, with a budget alert.
