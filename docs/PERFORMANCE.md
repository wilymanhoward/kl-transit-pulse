# Performance

## Index on `vehicle_positions(observed_at)`: EXPLAIN ANALYZE before and after

Reproduce: `docker compose exec -T db psql -U transit -d transit < scripts/perf_vehicle_positions.sql`

**Setup, and why it's synthetic.** On 2026-11-06 we had only hours of real data (~17.6k rows). To test at retention scale, the script builds a *temporary* table: the real rows repeated 112 times, each copy shifted 3 h later. That gives 14 days, **1.97 M rows, 206 MB**. The shape is real (same vehicles, routes, coordinates); the volume is synthetic. Postgres 16 on Docker Desktop, Windows 11 laptop, warm cache. Each query ran twice before measuring.

### 1. The estimator's "load one service day" query (`app/estimate.py` `load_runs`)

```sql
SELECT ... FROM vehicle_positions
WHERE observed_at >= '2026-11-12 00:00+08' AND observed_at < '2026-11-13 04:00+08'
  AND trip_start_date = '2026-11-12' AND trip_id IS NOT NULL
ORDER BY observed_at;   -- returns 140,944 rows
```

| | Plan | Rows read | Execution time |
|---|---|---|---|
| **Before** (no index) | Seq Scan + **external merge sort on disk** (11 MB spilled) | all 1.97 M (1.83 M thrown away) | **168 ms** |
| **After** (index on `observed_at`) | Index Scan, no sort needed (the index is already in time order) | 168k (27k thrown away) | **27 ms** |

About 6× faster. The bigger win is that it no longer reads the whole table, so the cost grows with one day's data instead of the full retention window.

### 2. Hourly retention delete (`app/realtime.py` `delete_old_positions`)

```sql
DELETE FROM vehicle_positions WHERE observed_at < <cutoff>;   -- deletes 144,704 rows
```

| | Plan | Execution time |
|---|---|---|
| **Before** | Seq Scan over all 1.97 M rows (plus a second full scan to find `max`) | **454 ms** |
| **After** | Index Scan on the old end of the index (and an index-only lookup for `max`) | **34 ms** |

About 13× faster. (Run inside `BEGIN ... ROLLBACK`, so the measurement deletes nothing.)

### What I didn't index, and why

- **`trip_start_date` in the index** (`(trip_start_date, observed_at)`): with the index above, only 27k of 168k rows are filtered out after the index lookup (runs from neighbouring service days in the same 28 h window). That's not worth a wider index that every insert would have to maintain.
- **An index for the Phase 4 API**: added with Phase 4, once the query existed and could be measured (below). It ended up as `(stop_id, service_date)`, not the `(stop_id, arrived_at)` I'd guessed, because the real query filters on service day.

## Index on `stop_arrivals(stop_id, service_date)` (Phase 4)

Reproduce: `docker compose exec -T db psql -U transit -d transit < scripts/perf_stop_arrivals.sql`

`/stops/{stop_id}/next` looks up the typical delay at one stop over the last 14 days. Same method as above: the real `stop_arrivals` rows repeated into a temporary 14-day table of **606k rows (35 MB, 1,797 stops)**, tested on the busiest real stop (`1001788`).

```sql
SELECT shape_id, stop_sequence, (trip_start_secs / 3600) % 24 AS hour,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY delay_secs), count(*)
FROM stop_arrivals
WHERE stop_id = $1 AND service_date >= $2 AND delay_secs IS NOT NULL
GROUP BY 1, 2, 3;
```

| | Plan | Rows read | Execution time |
|---|---|---|---|
| **Before** | Seq Scan | 606k (604,912 thrown away) | **35.4 ms** |
| **After** | Bitmap Index Scan → Bitmap Heap Scan | 2,240 | **3.8 ms** |

About 9× faster, and the cost now depends on one stop's history, not the whole table.

*A mistake I caught:* the first version of the script computed `max(service_date)` inside the query. That full-table subquery took ~60 ms in **both** runs and hid most of the difference. The endpoint passes a fixed date, so the script now does the same.

## API response times (local, real data)

Measured with curl against `127.0.0.1` (using `localhost` adds ~0.2 s on Windows while curl resolves IPv6 first, which is client-side and not the server):

| Request | Time |
|---|---|
| `/stops/{id}/next`, first call (loads the timetable once per process) | ~0.7 s |
| `/stops/{id}/next`, cached (15 s TTL) | 2–4 ms |
| `/routes/{id}/reliability`, uncached | ~7 ms |

### Estimator cost on real data

One service day of real data (407 runs, ~5.4k arrivals, 2.6 h of collection) takes about 1.4 s end to end: load, detection, headways, coverage, writes and stats rebuild. A full day will have roughly 8× more runs. The worker re-estimates today and yesterday every 15 min, which could start delaying polls once it takes tens of seconds; there's a `shortcut:` note in `app/worker.py` to move it to its own process if that happens.
