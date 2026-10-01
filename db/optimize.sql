-- Applied by migration 002 after measuring EXPLAIN ANALYZE (see docs/query_plans/).
-- Q1 top-risk vehicles per fleet/day
CREATE INDEX IF NOT EXISTS ix_vehicle_fleet ON vehicle (fleet_id) INCLUDE (vin);
CREATE INDEX IF NOT EXISTS ix_risk_day_p ON risk_score (scored_on, p_fail_7d DESC) INCLUDE (vehicle_id);
-- partial index: only OPEN alerts are counted on dashboards (~5% of rows)
CREATE INDEX IF NOT EXISTS ix_alert_open_vehicle ON alert (vehicle_id) WHERE status = 'OPEN';
-- Q2 keyset pagination of alerts (tenant, newest first)
CREATE INDEX IF NOT EXISTS ix_alert_fleet_ts ON alert (fleet_id, ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_alert_sev_ts ON alert (severity, ts DESC, id DESC);
-- Q3 fleet daily summary: pre-aggregated, refreshed CONCURRENTLY by the nightly batch job
CREATE MATERIALIZED VIEW IF NOT EXISTS fleet_daily_summary AS
  SELECT v.fleet_id, d.day, count(*) AS vehicles_reporting, sum(d.km) AS km,
         sum(d.idle_hours) AS idle_hours, sum(d.breakdown::int) AS breakdowns,
         sum(d.severe_dtc_cnt) AS severe_dtcs, avg(d.coolant_max) AS avg_coolant_max
  FROM vehicle_daily d JOIN vehicle v ON v.id = d.vehicle_id
  GROUP BY v.fleet_id, d.day;
CREATE UNIQUE INDEX IF NOT EXISTS ux_fds ON fleet_daily_summary (fleet_id, day);
ANALYZE;
