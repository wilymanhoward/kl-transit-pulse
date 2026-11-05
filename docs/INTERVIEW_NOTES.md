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
