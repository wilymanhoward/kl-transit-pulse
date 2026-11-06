-- EXPLAIN ANALYZE: ix_stop_arrivals_stop_id_service_date, for /stops/{id}/next.
--
-- Builds a TEMPORARY table from the real stop_arrivals rows, repeated 8x per day (a few
-- hours of real data ~ one eighth of a day) for 14 days. Nothing permanent is changed.
--
-- Run: docker compose exec -T db psql -U transit -d transit < scripts/perf_stop_arrivals.sql

CREATE TEMP TABLE sa AS
SELECT a.stop_id, a.shape_id, a.stop_sequence, a.trip_start_secs, a.delay_secs,
       a.service_date - (k / 8) AS service_date
FROM stop_arrivals a, generate_series(0, 111) AS k;
ANALYZE sa;

\echo == table size
SELECT count(*) AS rows, count(DISTINCT stop_id) AS stops,
       pg_size_pretty(pg_total_relation_size('sa')) AS size FROM sa;

-- The busiest real stop, so the comparison is the realistic worst case.
SELECT stop_id AS busy_stop FROM stop_arrivals GROUP BY 1 ORDER BY count(*) DESC LIMIT 1 \gset
-- Like the endpoint, filter on a constant date (it passes `since` as a parameter).
SELECT max(service_date) - 14 AS since FROM sa \gset

PREPARE typical(text, date) AS
SELECT shape_id, stop_sequence, (trip_start_secs / 3600) % 24 AS hour,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY delay_secs) AS median, count(*) AS n
FROM sa
WHERE stop_id = $1 AND service_date >= $2
  AND delay_secs IS NOT NULL
GROUP BY 1, 2, 3;

\echo == BEFORE: no index
\o /dev/null
EXECUTE typical(:'busy_stop', :'since');
EXECUTE typical(:'busy_stop', :'since');
\o
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON) EXECUTE typical(:'busy_stop', :'since');

CREATE INDEX sa_stop ON sa (stop_id, service_date);
ANALYZE sa;
DEALLOCATE typical;
PREPARE typical(text, date) AS
SELECT shape_id, stop_sequence, (trip_start_secs / 3600) % 24 AS hour,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY delay_secs) AS median, count(*) AS n
FROM sa
WHERE stop_id = $1 AND service_date >= $2
  AND delay_secs IS NOT NULL
GROUP BY 1, 2, 3;

\echo == AFTER: index on (stop_id, service_date)
\o /dev/null
EXECUTE typical(:'busy_stop', :'since');
\o
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON) EXECUTE typical(:'busy_stop', :'since');
