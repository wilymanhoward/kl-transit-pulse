# Data findings (Phase 0)

Source: one snapshot of `rapid-bus-kl` from data.gov.my, fetched 2026-11-05 10:27 UTC (18:27 Kuala Lumpur time, weekday evening peak). Reproduce with `scripts/fetch_samples.py` then `scripts/inspect_samples.py`.

**Caveat:** everything realtime below comes from one snapshot. Phase 2 will confirm it over many snapshots.

## Realtime vehicle positions

| Fact | Value |
|---|---|
| Vehicles in feed | 88 (53 distinct routes) |
| Header fields | `gtfs_realtime_version`, `incrementality`, `timestamp` |
| Header timestamp | 29 s older than fetch time |
| Populated on 100% of vehicles | `trip.trip_id`, `trip.route_id`, `trip.start_time`, `trip.start_date`, `position.latitude/longitude/bearing/speed`, `timestamp`, `vehicle.id`, `vehicle.license_plate` |
| **Never populated** | `stop_id`, `current_stop_sequence`, `current_status`, `congestion_level`, `occupancy_status`, `trip.direction_id`, `vehicle.label`, `position.odometer` |
| Vehicle timestamp age at fetch | min 33 s, median 49 s, max 101 s; none older than 5 min |
| `vehicle.id` | Same value as the licence plate (e.g. `WUY9579`) |
| Feed size | ~9.7 KB per snapshot |

Example `trip`: `trip_id "weekday_T2000_T200002_0"`, `start_time "18:05:15"`, `start_date "20261105"`, `route_id "T2000"`.

## Static GTFS

| Fact | Value |
|---|---|
| Files | `agency, calendar, frequencies, routes, shapes, stop_times, stops, trips` (no `calendar_dates.txt`) |
| Counts | 139 routes, 4,106 stops, 2,121 trips, 88,827 stop_times |
| Timezone | `Asia/Kuala_Lumpur` (agency.txt) |
| Service calendars | Only two: `weekday` (Mon to Fri) and `weekend` (Sat and Sun), valid 2020-04-01 to 2027-03-31. **No public-holiday exceptions.** |
| Times over 24:00:00 | Yes: 931 stop_times rows, max `25:15` |
| Trips missing from stop_times | 3 of 2,121 (0.1%), lower than the ~2% the docs mention |
| `trip_id` format | `{service}_{route}_{route+direction}_{index}`, e.g. `weekday_S1040_S104002_0` |

## Live vs static matching

| Check | Result |
|---|---|
| Live `route_id` found in `routes.txt` | 88/88 (100%) |
| Live `trip_id` found in `trips.txt` | 75/88 (85%) |
| Live `trip.start_time` inside that trip's frequency window | 37/88 |
| Live `trip.start_time` exactly on the headway grid | 2/88 |

Unmatched live trip IDs look like real-pattern IDs with an index the static file lacks (e.g. `weekday_U2500_U250001_22`).

## What breaks the plan

### 1. There is no fixed timetable to measure delay against (the big one)
`frequencies.txt` covers 2,118 of 2,121 trips, and every row has `exact_times = 0`. In GTFS that means **headway-based service**: "a bus roughly every 30 min between 06:00 and 19:30", not "a bus at 18:05". The `stop_times.txt` rows are a **template** (the first stop is usually `06:00:00`), giving travel-time offsets between stops, not real departure times.

So the brief's formula `delay = observed time − scheduled time` has no scheduled time to use. Headways are long, too: median 1,800 s (30 min), range 5 min to 3.5 h.

The live `trip.start_time` (e.g. `18:05:15`) looks like the actual start of that run, but only 2 of 88 sit on the headway grid. It does not give a usable scheduled time either.

### 2. The feed never says which stop a bus is at
`stop_id`, `current_stop_sequence` and `current_status` are never set. Stop arrivals have to come from GPS distance to stops (the brief's radius idea), using `shapes.txt` and the trip's stop list.

### 3. 15% of live trips can't be joined to the timetable
`route_id` always matches, so route-level stats still work for those vehicles. Trip-level logic must handle a missing trip without crashing.

### 4. Smaller things
- The API redirects `/prasarana?...` to `/prasarana/?...` (301), and the static ZIP then 302-redirects to S3. Without the trailing slash, each poll costs 2 requests against the 4/min limit. No rate-limit headers are returned.
- No `direction_id` in realtime. Get it from the static trip, or from the `01`/`02` suffix in the trip ID (unverified guess).
- No holiday calendar: on public holidays the "expected" service is wrong.
- Vehicle positions are 30 to 100 s old when we fetch them. Each one has its own `timestamp`, so we store that, not the fetch time.

## Proposed change to Phase 3 (needs Howard's OK)

Measure reliability the way transit agencies do for frequent, headway-based service:

1. **Headway adherence / excess wait time (main metric).** Detect arrivals at stops from GPS. For each stop and route, measure the real gap between buses and compare it with the scheduled headway for that time of day. "Scheduled every 15 min, actually 15 / 5 / 40 min apart" shows bunching and long waits. This is the standard metric for frequency-based service (Transport for London uses Excess Wait Time).
2. **Running-time adherence (secondary).** Compare how long a bus takes between stops with the template offsets in `stop_times.txt`, for example "the timetable allows 22 min from A to B, buses took 31 min at 6 pm".

Both are honest estimates from GPS, and both work without a fixed timetable. The API in Phase 4 would then report "on-time %" as "% of gaps within X min of the scheduled headway". `/stops/{id}/next` would become "expected next bus" from live positions plus the headway, not a list of timetabled departures.

## Phase 2 observations (live worker, 2026-11-06 12:17 to 12:20 Kuala Lumpur time)

From 6 consecutive polls, 30 s apart. A short window, so these are early signs, not conclusions.

| Fact | Value |
|---|---|
| Vehicles per snapshot | 87 to 93 (midday weekday) |
| Header timestamp | new on every poll, 30 s apart |
| Feed age when fetched (header vs our clock) | 1 to 5 s (on Nov 5 it was 29 s) |
| Buses repeating an already-seen GPS fix | 10 to 33% of each snapshot (dropped by `ON CONFLICT`) |
| Entities dropped as invalid | 0 |
| Storage | ~70 new rows per poll, about 200k rows/day, about 2.8M rows at 14-day retention |

The Phase 1 worry (two downloads 20 min apart with the same byte size) didn't show up here: every snapshot had a new header. The stale/duplicate check stays in place regardless.

Most buses (67 to 90% per poll) send a new fix every 30 s poll; the rest are slower. At ~10 m/s that means about 300 m or more between fixes, much more than the 75 m stop radius in the original brief. **Phase 3 can't rely on catching a bus within 75 m of a stop.** It will need to interpolate between two fixes (when did the bus pass the stop?) rather than wait for a fix near it. Phase 3 should confirm the fix interval over more data first.

## Phase 3 findings (2.6 h of live data, 13,720 positions, 332 runs, 2026-11-06 midday)

| Fact | Value | Consequence |
|---|---|---|
| Gap between a bus's fixes | p25 30 s, median 30 s, p75 55 s, p90 60 s, p99 ~19 min | Usually ~300 m between fixes |
| Distance between consecutive stops | p10 176 m, median 343 m, p90 793 m | A bus often passes a stop *between* two fixes, so arrival time must be interpolated |
| Trips that are loops (first stop = last stop) | 956 of 2,121 | Matching must follow stop order, or a loop's end looks like its start |
| Trips visiting the same stop twice | 1,024 | Same reason; headways are grouped by stop *sequence*, not just stop ID |
| `trip_id` contains the `shape_id` (`weekday_U7830_U783002_2`) | 2,121 of 2,121 | Unknown live trips can borrow the stop list of a timetable trip with the same shape |
| `trip_id` starts with the `service_id` | 2,121 of 2,121 | Weekday/weekend known even for unknown trips |
| Positions whose trip_id isn't in the timetable | 199 of 13,720 (1.5%) | Much lower than the 15% from the single snapshot |
| Live `start_time` vs first GPS fix | median 0.4 min later | `start_time` is the actual departure time of that run |
| Live `start_time` inside its own trip's frequency window | 112 of 206 runs (54%) | The trip ID's `_N` index doesn't reliably say which window applies... |
| ...inside any window of the same shape | 182 of 206 (88%) | ...so look up the scheduled headway by shape + departure time |
| Frequency windows overlapping within one route+direction | 35 pairs | Combine overlapping windows: 1/h = 1/h1 + 1/h2 |
| Gaps between windows (e.g. 07:00–07:30) | common | No scheduled headway, so those gaps aren't judged |
| Ingestion failures | 5 DNS errors + one 59-min gap (the PC lost network / slept) | Headways that span an outage would look like huge waits, so they're excluded |

### The realtime feed only covers about a quarter to a third of scheduled buses (changes the plan)

Weekday 13:00–14:00 KL, 2026-11-06:

| | Timetable | Seen in realtime feed |
|---|---|---|
| Routes with service | 109 | 46 |
| Departures (runs starting that hour) | 362 | 96 (27%) |
| Routes with zero buses in the feed | – | 73 of 109 (including the BRT Sunway Line B1000, every 3 min) |
| Routes with >= 80% of their departures seen | – | 11 |

*Correction:* a first version of this table said 285 departures (34%). That query only counted frequency windows covering the *whole* hour. Counting every generated departure (`generate_series` in SQL, and `departures_per_hour` in code, which agree) gives 362.

Cross-check: about 90 vehicles report at any moment, while the timetable needs roughly 240 on the road at midday.

**Consequence:** a headway is the gap between buses *we can see*. If most of a route's buses have no GPS in the feed, an every-5-min route looks like every-15-min. From this data alone, "bus didn't run" and "bus ran without GPS" are impossible to tell apart. Headway statistics are only trustworthy where coverage is near 100%.

**What does work: delay per bus, against its own departure.** The live `start_time` is the run's actual departure. For 175 of 355 runs (49%), the first GPS fix arrives within 3 min of it. Adding the timetable's travel time to each stop (from `stop_times`) gives a scheduled arrival per stop, so `delay = observed arrival − (departure + scheduled travel time)`. Missing buses don't distort it. On the 2.6 h sample:

- All runs: median delay 5.1 min, but p95 = 201 min. That's garbage from runs first seen long after their `start_time`.
- Runs seen at departure: median 4.5 min, p95 17.5 min. Delay grows along the trip (median 2.3 min in the first 15 scheduled minutes, about 10 min later on), which fits midday traffic.
- Buses' GPS gets a median 137 m from the timetable's *first* stop (terminal bays aren't at the stop pin), so "detected at the first stop" is not a usable filter. "First fix within 3 min of `start_time`" is.
