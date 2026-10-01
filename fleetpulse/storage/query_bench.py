"""EXPLAIN ANALYZE before/after for the three slowest dashboard queries.
Writes docs/query_plans/*.txt and docs/query_plans/summary.json."""
from __future__ import annotations

import json
import os
import re
import statistics

import psycopg

from fleetpulse.storage.seed_pg import DSN

OUT = "docs/query_plans"
Q = {
    "Q1_top_risk": (
        """SELECT v.vin, r.p_fail_7d, r.top_factors,
              (SELECT count(*) FROM alert a WHERE a.vehicle_id = v.id AND a.status = 'OPEN') AS open_alerts
           FROM risk_score r JOIN vehicle v ON v.id = r.vehicle_id
           WHERE v.fleet_id = 7 AND r.scored_on = '2026-08-29'
           ORDER BY r.p_fail_7d DESC LIMIT 50""",
        None),
    "Q2_alert_page": (
        """SELECT id, vehicle_id, ts, kind, detail FROM alert WHERE severity = 'CRITICAL'
           ORDER BY ts DESC, id DESC OFFSET 20000 LIMIT 50""",
        """SELECT id, vehicle_id, ts, kind, detail FROM alert WHERE severity = 'CRITICAL'
             AND (ts, id) < (%(ts)s, %(id)s) ORDER BY ts DESC, id DESC LIMIT 50"""),
    "Q3_fleet_trend": (
        """SELECT d.day, count(*), sum(d.km), sum(d.idle_hours), sum(d.breakdown::int), avg(d.coolant_max)
           FROM vehicle_daily d JOIN vehicle v ON v.id = d.vehicle_id
           WHERE v.fleet_id = 7 GROUP BY d.day ORDER BY d.day""",
        """SELECT day, vehicles_reporting, km, idle_hours, breakdowns, avg_coolant_max
           FROM fleet_daily_summary WHERE fleet_id = 7 ORDER BY day"""),
}


def run(cur, sql, params=None, reps=5):
    times, plan = [], ""
    for _ in range(reps):
        rows = cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, params).fetchall()
        plan = "\n".join(r[0] for r in rows)
        times.append(float(re.search(r"Execution Time: ([\d.]+) ms", plan).group(1)))
    return statistics.median(times), plan


def main():
    os.makedirs(OUT, exist_ok=True)
    res = {}
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        for stmt in ["DROP MATERIALIZED VIEW IF EXISTS fleet_daily_summary", "DROP INDEX IF EXISTS ix_vehicle_fleet",
                     "DROP INDEX IF EXISTS ix_risk_day_p", "DROP INDEX IF EXISTS ix_alert_open_vehicle",
                     "DROP INDEX IF EXISTS ix_alert_fleet_ts", "DROP INDEX IF EXISTS ix_alert_sev_ts"]:
            cur.execute(stmt)
        cur.execute("ANALYZE")
        before = {k: run(cur, q) for k, (q, _) in Q.items()}
        cur.execute(open("db/optimize.sql").read())
        # keyset cursor = last row of the page before OFFSET 20000
        ts, id_ = cur.execute("""SELECT ts, id FROM alert WHERE severity='CRITICAL'
                                  ORDER BY ts DESC, id DESC OFFSET 19999 LIMIT 1""").fetchone()
        for k, (q, q2) in Q.items():
            ms_b, plan_b = before[k]
            ms_a, plan_a = run(cur, q2 or q, {"ts": ts, "id": id_} if q2 and "%(" in q2 else None)
            res[k] = {"before_ms": ms_b, "after_ms": ms_a, "speedup": round(ms_b / ms_a, 1)}
            with open(f"{OUT}/{k}.txt", "w") as fh:
                fh.write(f"-- BEFORE ({ms_b} ms)\n{q}\n\n{plan_b}\n\n-- AFTER ({ms_a} ms)\n{q2 or q}\n\n{plan_a}\n")
    json.dump(res, open(f"{OUT}/summary.json", "w"), indent=2)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
