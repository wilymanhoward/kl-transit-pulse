-- EXPLAIN ANALYZE: what ix_vehicle_positions_observed_at buys at 14-day retention scale.
--
-- We only have hours of real data, so this builds a TEMPORARY table by repeating the real
-- vehicle_positions rows 112 times, each copy shifted 3 h later (14 days x 8 blocks).
-- Synthetic in volume, real in shape. Nothing permanent is created or changed.
--
-- Run: docker compose exec -T db psql -U transit -d transit < scripts/perf_vehicle_positions.sql

CREATE TEMP TABLE vp AS
SELECT v.vehicle_id, v.trip_id, v.route_id, v.trip_start_secs, v.latitude, v.longitude,
       v.observed_at + k * interval '3 hours' AS observed_at,
       ((v.observed_at + k * interval '3 hours') AT TIME ZONE 'Asia/Kuala_Lumpur')::date
           AS trip_start_date
FROM vehicle_positions v, generate_series(0, 111) AS k;
ANALYZE vp;

\echo == table size
SELECT count(*) AS rows, min(observed_at) AS first, max(observed_at) AS last,
       pg_size_pretty(pg_total_relation_size('vp')) AS size
FROM vp;

-- The estimator's per-service-day load (app/estimate.py load_runs), for a day mid-table.
PREPARE load_day AS
SELECT vehicle_id, trip_id, route_id, trip_start_secs, observed_at, latitude, longitude
FROM vp
WHERE observed_at >= '2026-11-12 00:00+08' AND observed_at < '2026-11-13 04:00+08'
  AND trip_start_date = '2026-11-12' AND trip_id IS NOT NULL
ORDER BY observed_at;

\echo == BEFORE: no index on observed_at
\o /dev/null
EXECUTE load_day;  -- warm the cache so both runs are compared fairly
EXECUTE load_day;
\o
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON) EXECUTE load_day;

CREATE INDEX vp_observed_at ON vp (observed_at);
ANALYZE vp;
DEALLOCATE load_day;
PREPARE load_day AS
SELECT vehicle_id, trip_id, route_id, trip_start_secs, observed_at, latitude, longitude
FROM vp
WHERE observed_at >= '2026-11-12 00:00+08' AND observed_at < '2026-11-13 04:00+08'
  AND trip_start_date = '2026-11-12' AND trip_id IS NOT NULL
ORDER BY observed_at;

\echo == AFTER: index on observed_at
\o /dev/null
EXECUTE load_day;
\o
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON) EXECUTE load_day;

\echo == retention delete (rows older than 14 days before the end of the table), BEFORE vs AFTER
DROP INDEX vp_observed_at;
BEGIN;
EXPLAIN (ANALYZE, COSTS OFF, SUMMARY ON)
DELETE FROM vp WHERE observed_at < (SELECT max(observed_at) FROM vp) - interval '13 days';
ROLLBACK;
CREATE INDEX vp_observed_at ON vp (observed_at);
ANALYZE vp;
BEGIN;
EXPLAIN (ANALYZE, COSTS OFF, SUMMARY ON)
DELETE FROM vp WHERE observed_at < (SELECT max(observed_at) FROM vp) - interval '13 days';
ROLLBACK;
