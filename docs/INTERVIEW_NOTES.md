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
