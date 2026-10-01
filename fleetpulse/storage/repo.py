"""PostgreSQL repositories (infrastructure layer). All queries are parameterised
(no string-built SQL from user input) and tenant-filtered by the caller's scope.
Pagination is keyset-based; list queries fetch related data in one statement
(LATERAL / aggregate) to avoid ORM-style N+1 round trips."""
from __future__ import annotations

import base64
import json
from datetime import date, datetime

from psycopg.rows import dict_row


def enc_cursor(*vals) -> str:
    return base64.urlsafe_b64encode(json.dumps(vals, default=str).encode()).decode()


def dec_cursor(c: str | None):
    if not c:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(c.encode()))
    except Exception as e:  # noqa: BLE001
        raise ValueError("bad cursor") from e


class Repo:
    def __init__(self, pool):
        self.pool = pool

    def latest_score_date(self, conn) -> date:
        return conn.execute("SELECT max(scored_on) AS d FROM risk_score").fetchone()["d"]

    def at_risk(self, fleet_id: int | None, limit: int, cursor: str | None, on: date | None = None):
        with self.pool.connection() as conn:
            conn.row_factory = dict_row
            on = on or self.latest_score_date(conn)
            cur = dec_cursor(cursor)
            params = {"on": on, "fleet": fleet_id, "lim": limit + 1}
            where = "r.scored_on = %(on)s AND (%(fleet)s::int IS NULL OR v.fleet_id = %(fleet)s)"
            if cur:
                where += " AND (r.p_fail_7d, r.vehicle_id) < (%(cp)s, %(cv)s)"
                params.update(cp=cur[0], cv=cur[1])
            rows = conn.execute(f"""
              SELECT r.vehicle_id, v.vin, v.fleet_id, r.p_fail_7d, r.top_factors, m.powertrain, o.code AS oem,
                     m.model_year, d.city, coalesce(a.open_alerts, 0) AS open_alerts
              FROM risk_score r
              JOIN vehicle v ON v.id = r.vehicle_id
              JOIN vehicle_model m ON m.id = v.model_id
              JOIN oem o ON o.id = m.oem_id
              LEFT JOIN depot d ON d.id = v.home_depot_id
              LEFT JOIN LATERAL (SELECT count(*) AS open_alerts FROM alert al
                                 WHERE al.vehicle_id = v.id AND al.status = 'OPEN') a ON true
              WHERE {where}
              ORDER BY r.p_fail_7d DESC, r.vehicle_id DESC LIMIT %(lim)s""", params).fetchall()
        nxt = None
        if len(rows) > limit:
            rows = rows[:limit]
            nxt = enc_cursor(rows[-1]["p_fail_7d"], rows[-1]["vehicle_id"])
        return {"scored_on": str(on), "items": rows, "next_cursor": nxt}

    def vehicle(self, vin: str, fleet_id: int | None):
        with self.pool.connection() as conn:
            conn.row_factory = dict_row
            v = conn.execute("""
              SELECT v.id, v.vin, v.fleet_id, m.powertrain, m.model_year, o.code AS oem, d.name AS depot, d.city,
                     d.lat AS depot_lat, d.lon AS depot_lon
              FROM vehicle v JOIN vehicle_model m ON m.id = v.model_id JOIN oem o ON o.id = m.oem_id
              LEFT JOIN depot d ON d.id = v.home_depot_id
              WHERE v.vin = %s AND (%s::int IS NULL OR v.fleet_id = %s)""", (vin, fleet_id, fleet_id)).fetchone()
            if not v:
                return None
            v["risk_history"] = conn.execute(
                "SELECT scored_on, p_fail_7d, top_factors FROM risk_score WHERE vehicle_id = %s ORDER BY scored_on",
                (v["id"],)).fetchall()
            v["daily"] = conn.execute("""
              SELECT day, km, coolant_max, aux_v_min, oil_kpa_min, misfire_cnt, cell_temp_spread, soc_drop_per100,
                     severe_dtc_cnt, dtcs, breakdown FROM vehicle_daily WHERE vehicle_id = %s ORDER BY day""",
                                      (v["id"],)).fetchall()
            v["alerts"] = conn.execute("""SELECT id, ts, kind, severity, detail, status FROM alert
                                          WHERE vehicle_id = %s ORDER BY ts DESC LIMIT 20""", (v["id"],)).fetchall()
            v["work_orders"] = conn.execute("""SELECT id, status, reason, scheduled_for, proposed_by, approved_by
                                              FROM work_order WHERE vehicle_id = %s ORDER BY id DESC""",
                                            (v["id"],)).fetchall()
            return v

    def alerts(self, fleet_id, limit, cursor, severity=None, status=None):
        cur = dec_cursor(cursor)
        params = {"fleet": fleet_id, "sev": severity, "st": status, "lim": limit + 1}
        where = ["(%(fleet)s::int IS NULL OR a.fleet_id = %(fleet)s)",
                 "(%(sev)s::text IS NULL OR a.severity = %(sev)s)", "(%(st)s::text IS NULL OR a.status = %(st)s)"]
        if cur:
            where.append("(a.ts, a.id) < (%(cts)s, %(cid)s)")
            params.update(cts=datetime.fromisoformat(cur[0]), cid=cur[1])
        with self.pool.connection() as conn:
            conn.row_factory = dict_row
            rows = conn.execute(f"""SELECT a.id, v.vin, a.fleet_id, a.ts, a.kind, a.severity, a.detail, a.status
                                    FROM alert a JOIN vehicle v ON v.id = a.vehicle_id
                                    WHERE {' AND '.join(where)} ORDER BY a.ts DESC, a.id DESC LIMIT %(lim)s""",
                                params).fetchall()
        nxt = None
        if len(rows) > limit:
            rows = rows[:limit]
            nxt = enc_cursor(rows[-1]["ts"].isoformat(), rows[-1]["id"])
        return {"items": rows, "next_cursor": nxt}

    def ack_alert(self, alert_id: int, fleet_id):
        with self.pool.connection() as conn:
            r = conn.execute("""UPDATE alert SET status='ACK' WHERE id=%s AND status='OPEN'
                                AND (%s::int IS NULL OR fleet_id=%s) RETURNING id""", (alert_id, fleet_id, fleet_id))
            return r.fetchone() is not None

    def insert_alert(self, conn, vehicle_vin: str, kind, severity, detail, value, ts: datetime, dedup_key: str):
        """Idempotent (ON CONFLICT DO NOTHING on dedup_key) - safe under at-least-once delivery."""
        return conn.execute("""
          INSERT INTO alert (vehicle_id, fleet_id, ts, kind, severity, detail, value, dedup_key)
          SELECT id, fleet_id, %s, %s, %s, %s, %s, %s FROM vehicle WHERE vin = %s
          ON CONFLICT (dedup_key) DO NOTHING RETURNING id, fleet_id""",
                            (ts, kind, severity, detail, value, dedup_key, vehicle_vin)).fetchone()

    def fleet_summary(self, fleet_id):
        with self.pool.connection() as conn:
            conn.row_factory = dict_row
            trend = conn.execute("""SELECT day, sum(vehicles_reporting) AS vehicles_reporting, sum(km) AS km,
                                      sum(breakdowns) AS breakdowns, sum(severe_dtcs) AS severe_dtcs
                                    FROM fleet_daily_summary WHERE (%s::int IS NULL OR fleet_id = %s)
                                    GROUP BY day ORDER BY day""", (fleet_id, fleet_id)).fetchall()
            on = self.latest_score_date(conn)
            risk = conn.execute("""SELECT count(*) FILTER (WHERE r.p_fail_7d >= 0.5) AS high,
                                          count(*) FILTER (WHERE r.p_fail_7d >= 0.15 AND r.p_fail_7d < 0.5) AS medium,
                                          count(*) AS total
                                   FROM risk_score r JOIN vehicle v ON v.id = r.vehicle_id
                                   WHERE r.scored_on = %s AND (%s::int IS NULL OR v.fleet_id = %s)""",
                                (on, fleet_id, fleet_id)).fetchone()
            open_alerts = conn.execute("""SELECT count(*) AS n FROM alert WHERE status='OPEN'
                                          AND (%s::int IS NULL OR fleet_id = %s)""", (fleet_id, fleet_id)).fetchone()
        return {"trend": trend, "risk": risk, "open_alerts": open_alerts["n"], "scored_on": str(on)}

    def create_work_order(self, vehicle_id, reason, proposed_by, idem_key, depot_id=None, scheduled_for=None):
        with self.pool.connection() as conn:
            conn.row_factory = dict_row
            row = conn.execute("""
              INSERT INTO work_order (vehicle_id, depot_id, reason, proposed_by, scheduled_for, idempotency_key)
              VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (idempotency_key) DO NOTHING
              RETURNING id, status, vehicle_id""", (vehicle_id, depot_id, reason, proposed_by, scheduled_for,
                                                    idem_key)).fetchone()
            if row is None:  # replay of the same request -> return original
                row = conn.execute("SELECT id, status, vehicle_id FROM work_order WHERE idempotency_key=%s",
                                   (idem_key,)).fetchone()
                row["replayed"] = True
            return row

    def approve_work_order(self, wo_id, approver, fleet_id, approve=True):
        with self.pool.connection() as conn:
            conn.row_factory = dict_row
            return conn.execute("""
              UPDATE work_order w SET status = %s, approved_by = %s
              FROM vehicle v WHERE w.id = %s AND v.id = w.vehicle_id AND w.status = 'PROPOSED'
                AND (%s::int IS NULL OR v.fleet_id = %s) RETURNING w.id, w.status""",
                                ("APPROVED" if approve else "REJECTED", approver, wo_id, fleet_id, fleet_id)).fetchone()

    def erase_driver(self, driver_id: int, fleet_id, requested_by: str):
        """Right to erasure (GDPR Art.17 / DPDP s.12): pseudonym + licence hash overwritten,
        assignments detached from the person; vehicle telemetry is retained as non-personal fleet data."""
        with self.pool.connection() as conn, conn.transaction():
            d = conn.execute("SELECT id FROM driver WHERE id=%s AND (%s::int IS NULL OR fleet_id=%s) AND erased_at IS NULL",
                             (driver_id, fleet_id, fleet_id)).fetchone()
            if not d:
                return None
            rid = conn.execute("INSERT INTO erasure_request (subject_type, subject_id, requested_by) "
                               "VALUES ('driver', %s, %s) RETURNING id", (driver_id, requested_by)).fetchone()[0]
            conn.execute("UPDATE driver SET pseudonym='ERASED', licence_hash='\\x00', erased_at=now() WHERE id=%s",
                         (driver_id,))
            conn.execute("UPDATE trip SET driver_id=NULL WHERE driver_id=%s", (driver_id,))
            conn.execute("DELETE FROM vehicle_assignment WHERE driver_id=%s", (driver_id,))
            conn.execute("UPDATE erasure_request SET completed_at=now() WHERE id=%s", (rid,))
            return rid

    def depots(self, fleet_id):
        with self.pool.connection() as conn:
            conn.row_factory = dict_row
            return conn.execute("SELECT id, name, city, lat, lon, service_bays FROM depot WHERE fleet_id=%s",
                                (fleet_id,)).fetchall()
