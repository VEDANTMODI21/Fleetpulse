"""Load the seeded dataset into PostgreSQL (relational core + 30-day hot rollups),
score risk for the last 7 days, and derive historical alerts.

Usage: python -m fleetpulse.storage.seed_pg [--data data] [--hot-days 30]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import time
from datetime import date, timedelta

import duckdb
import numpy as np
import psycopg

from fleetpulse.ml.features import load_features
from fleetpulse.ml.registry import load_model
from fleetpulse.simulator.fleet import CITIES

DSN = os.environ.get("DATABASE_URL")
START = date(2026, 6, 1)
SEGMENTS = ["rental", "logistics", "ride_hailing", "lender", "dealer"]
DTC_CATALOG = [
    ("P0217", "powertrain", "Engine coolant over-temperature condition", "CRITICAL"),
    ("P0128", "powertrain", "Coolant thermostat below regulating temperature", "WARN"),
    ("P0562", "powertrain", "System voltage low", "WARN"),
    ("P0300", "powertrain", "Random/multiple cylinder misfire detected", "CRITICAL"),
    ("P0301", "powertrain", "Cylinder 1 misfire detected", "CRITICAL"),
    ("P0302", "powertrain", "Cylinder 2 misfire detected", "CRITICAL"),
    ("P0520", "powertrain", "Engine oil pressure sensor/switch circuit", "CRITICAL"),
    ("P0524", "powertrain", "Engine oil pressure too low", "CRITICAL"),
    ("P0A80", "powertrain", "Replace hybrid/EV battery pack", "CRITICAL"),
    ("P0AFA", "powertrain", "Hybrid/EV battery system voltage low", "CRITICAL"),
    ("P0118", "powertrain", "Engine coolant temperature sensor circuit high", "WARN"),
    ("P0420", "powertrain", "Catalyst system efficiency below threshold", "INFO"),
    ("P0455", "powertrain", "Evaporative emission system leak detected (large)", "INFO"),
    ("P0442", "powertrain", "Evaporative emission system leak detected (small)", "INFO"),
    ("P0171", "powertrain", "System too lean (bank 1)", "INFO"),
    ("B1000", "body", "ECU malfunction (manufacturer specific)", "INFO"),
    ("U0100", "network", "Lost communication with ECM/PCM", "WARN"),
]


def copy_rows(cur, table, cols, rows):
    with cur.copy(f"COPY {table} ({','.join(cols)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("DATA_DIR", "data"))
    ap.add_argument("--models", default=os.environ.get("MODEL_DIR", "models"))
    ap.add_argument("--hot-days", type=int, default=30)
    a = ap.parse_args(argv)
    t0 = time.time()
    con = duckdb.connect()
    veh = con.sql(f"select * from read_parquet('{a.data}/vehicles.parquet') order by vid").df()
    last_day = con.sql(f"select max(day) from read_parquet('{a.data}/daily/*.parquet')").fetchone()[0]
    first_hot = last_day - a.hot_days + 1

    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(open(os.path.join(os.path.dirname(__file__), "../../db/schema.sql")).read())
        for m in range(6, 10):  # monthly partitions Jun..Sep 2026
            lo, hi = date(2026, m, 1), date(2026, m + 1, 1)
            cur.execute(f"CREATE TABLE vehicle_daily_2026_{m:02d} PARTITION OF vehicle_daily "
                        f"FOR VALUES FROM ('{lo}') TO ('{hi}')")
        n_fleets = int(veh.fleet_id.max())
        copy_rows(cur, "fleet", ["id", "name", "segment"],
                  [(i, f"Fleet-{i:03d}", SEGMENTS[i % 5]) for i in range(1, n_fleets + 1)])
        copy_rows(cur, "subscription", ["fleet_id", "plan", "vehicle_limit", "valid_from"],
                  [(i, ["basic", "pro", "enterprise"][i % 3], 10_000, date(2026, 1, 1)) for i in range(1, n_fleets + 1)])
        users = [("admin@fleetpulse.dev", None, "platform_admin")]
        for i in range(1, n_fleets + 1):
            users += [(f"manager{i}@fleet{i}.dev", i, "fleet_manager"), (f"tech{i}@fleet{i}.dev", i, "technician")]
        copy_rows(cur, "app_user", ["email", "fleet_id", "role"], users)
        copy_rows(cur, "oem", ["id", "code", "name"], [(1, "alpha", "Alpha Motors"), (2, "beta", "Beta Auto"),
                                                         (3, "gamma", "Gamma Cars")])
        oem_id = {"alpha": 1, "beta": 2, "gamma": 3}
        combos = veh[["oem", "powertrain", "model_year"]].drop_duplicates().reset_index(drop=True)
        combos["id"] = np.arange(1, len(combos) + 1)
        copy_rows(cur, "vehicle_model", ["id", "oem_id", "name", "powertrain", "model_year"],
                  [(int(r.id), oem_id[r.oem], f"{r.oem.title()} {r.powertrain}", r.powertrain, int(r.model_year))
                   for r in combos.itertuples()])
        veh = veh.merge(combos, on=["oem", "powertrain", "model_year"])
        veh = veh.sort_values("vid")
        depots, depot_id = [], {}
        for f in range(1, n_fleets + 1):
            for city, (lat, lon) in CITIES.items():
                depot_id[(f, city)] = len(depots) + 1
                depots.append((len(depots) + 1, f, f"{city} Depot F{f}", city, lat, lon, 4 + f % 5))
        copy_rows(cur, "depot", ["id", "fleet_id", "name", "city", "lat", "lon", "service_bays"], depots)
        copy_rows(cur, "vehicle", ["id", "vin", "fleet_id", "model_id", "home_depot_id"],
                  [(int(r.vid) + 1, r.vin, int(r.fleet_id), int(r.id), depot_id[(int(r.fleet_id), r.city)])
                   for r in veh.itertuples()])
        salt = b"demo-salt"
        copy_rows(cur, "driver", ["id", "fleet_id", "pseudonym", "licence_hash"],
                  [(int(r.vid) + 1, int(r.fleet_id), f"DRV-{int(r.vid) + 1:06d}",
                    hashlib.sha256(salt + f"DL{r.vid:010d}".encode()).digest()) for r in veh.itertuples()])
        copy_rows(cur, "vehicle_assignment", ["vehicle_id", "driver_id", "from_ts"],
                  [(int(v) + 1, int(v) + 1, "2026-01-01T00:00:00Z") for v in veh.vid])
        copy_rows(cur, "dtc_catalog", ["code", "system", "description", "severity"], DTC_CATALOG)
        cur.execute("INSERT INTO oem_mapping (oem_id, version, mapping, active) VALUES (1,1,%s,true)",
                    (json.dumps({"format": "alpha-flat-v1"}),))
        t1 = time.time()

        daily = con.sql(f"""select vid+1 as vehicle_id, date, km, engine_hours, idle_hours, harsh_brakes, coolant_max,
                aux_v_min, oil_kpa_min, misfire_cnt, soc_drop_per100, cell_temp_spread, dtc_cnt, severe_dtc_cnt,
                nullif(dtcs,'') dtcs, breakdown=1 as breakdown
                from read_parquet('{a.data}/daily/*.parquet') where day >= {first_hot} and off_road = 0""").df()
        buf = io.StringIO()
        daily.to_csv(buf, index=False, header=False, na_rep="\\N")
        buf.seek(0)
        with cur.copy("COPY vehicle_daily FROM STDIN WITH (FORMAT csv, NULL '\\N')") as cp:
            while chunk := buf.read(1 << 20):
                cp.write(chunk)
        t2 = time.time()

        # historical alerts derived from rollups (what the stream layer would have emitted)
        alerts = con.sql(f"""
          select vid+1 vehicle_id, v.fleet_id, d.date, kind, sev, detail, val from (
            select vid, date, 'OVERHEAT' kind, 'CRITICAL' sev, 'Coolant max '||round(coolant_max,1)||'C' detail, coolant_max val
              from read_parquet('{a.data}/daily/*.parquet') where day >= {first_hot} and coolant_max > 112
            union all select vid, date, 'LOW_12V','WARN','12V min '||round(aux_v_min,2)||'V', aux_v_min
              from read_parquet('{a.data}/daily/*.parquet') where day >= {first_hot} and aux_v_min < 11.5
            union all select vid, date, 'SEVERE_DTC','CRITICAL','Severe DTC: '||dtcs, severe_dtc_cnt
              from read_parquet('{a.data}/daily/*.parquet') where day >= {first_hot} and severe_dtc_cnt > 0
            union all select vid, date, 'BREAKDOWN','CRITICAL','Vehicle reported breakdown', null
              from read_parquet('{a.data}/daily/*.parquet') where day >= {first_hot} and breakdown = 1
          ) d join read_parquet('{a.data}/vehicles.parquet') v using (vid)""").df()
        rng = np.random.default_rng(4)
        secs = rng.integers(0, 86400, len(alerts))
        alerts["date"] = alerts["date"].dt.date
        status = np.where(alerts.date < (START + timedelta(days=int(last_day) - 3)), "CLOSED", "OPEN")
        copy_rows(cur, "alert", ["vehicle_id", "fleet_id", "ts", "kind", "severity", "detail", "value", "status", "dedup_key"],
                  [(int(r.vehicle_id), int(r.fleet_id), f"{r.date}T{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}Z",
                    r.kind, r.sev, r.detail, None if r.val != r.val else float(r.val), st,
                    f"{r.vehicle_id}:{r.kind}:{r.date}")
                   for r, s, st in zip(alerts.itertuples(), secs, status)])
        t3 = time.time()

        # risk scores for last 7 days (batch scoring job)
        mdl = load_model(os.path.join(a.models, "gbm-v1.pkl"))
        feats = load_features(a.data, last_day - 6, last_day, con)
        p = mdl["model"].predict_proba(feats[mdl["features"]].to_numpy(np.float32))[:, 1]
        feats["p"] = p
        top = explain_rows(feats, mdl["features"])
        copy_rows(cur, "risk_score", ["vehicle_id", "scored_on", "p_fail_7d", "model_version", "top_factors"],
                  [(int(v) + 1, START + timedelta(days=int(d)), float(pp), mdl["version"], json.dumps(t))
                   for v, d, pp, t in zip(feats.vid, feats.day, feats.p, top)])
        cur.execute("ANALYZE")
        try:  # invalidate API caches keyed on the risk version
            import redis
            redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0")).incr("risk:version")
        except Exception as e:  # noqa: BLE001 - cache invalidation is best effort; TTL bounds staleness
            print(f"warning: cache invalidation skipped: {e!r}")
        t4 = time.time()
        counts = {t: cur.execute(f"select count(*) from {t}").fetchone()[0]
                  for t in ["vehicle", "driver", "vehicle_daily", "alert", "risk_score"]}
    print(json.dumps({"counts": counts, "core_s": round(t1 - t0, 1), "daily_copy_s": round(t2 - t1, 1),
                      "alerts_s": round(t3 - t2, 1), "scoring_s": round(t4 - t3, 1)}))


# Human-readable risk drivers: signal vs fleet-normal threshold, ranked by how far past it the vehicle is.
# (signal, threshold, direction(+1 = high is bad), scale, label)
NORMAL = [("coolant_max_max3", 104, 1, 8, "Coolant temperature running hot"),
          ("aux_v_min_min3", 11.9, -1, 0.4, "12V battery voltage dropping"),
          ("oil_kpa_min_min3", 140, -1, 30, "Oil pressure below normal"),
          ("misfire_cnt_max3", 3, 1, 8, "Engine misfires increasing"),
          ("cell_temp_spread_max3", 4.5, 1, 2, "HV battery cell temperature imbalance"),
          ("soc_drop_per100_mean7", 22, 1, 3, "Abnormal EV energy consumption"),
          ("sev_dtc_7d", 0, 1, 1, "Severe diagnostic trouble code(s) active")]


def explain_rows(df, features=None):
    cols = {k: df[k].to_numpy() for k, *_ in NORMAL}
    out = []
    for i in range(len(df)):
        reasons = []
        for k, thr, sign, scale, label in NORMAL:
            v = cols[k][i]
            if v == v and (v - thr) * sign > 0:
                reasons.append((((v - thr) * sign) / scale, {"factor": label, "signal": k, "value": round(float(v), 2)}))
        reasons.sort(key=lambda r: -r[0])
        out.append([r for _, r in reasons[:3]])
    return out


if __name__ == "__main__":
    main()
