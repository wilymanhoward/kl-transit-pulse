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
- **Indexes for the Phase 4 API** (`stop_arrivals(stop_id, arrived_at)` for "next buses at a stop"): added with Phase 4, when the query exists and can be measured.

### Estimator cost on real data

One service day of real data (407 runs, ~5.4k arrivals, 2.6 h of collection) takes about 1.4 s end to end: load, detection, headways, coverage, writes and stats rebuild. A full day will have roughly 8× more runs. The worker re-estimates today and yesterday every 15 min, which could start delaying polls once it takes tens of seconds; there's a `shortcut:` note in `app/worker.py` to move it to its own process if that happens.
