"""Nightly batch scoring job (idempotent): features from the lake -> model -> UPSERT risk_score,
refresh materialised views, bump the cache version so API caches invalidate.
python -m fleetpulse.ml.score --data /data --models /app/models [--day N]"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta

import duckdb
import numpy as np
import psycopg

from fleetpulse.ml.features import load_features
from fleetpulse.ml.registry import load_model
from fleetpulse.storage.seed_pg import START, explain_rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("DATA_DIR", "data"))
    ap.add_argument("--models", default=os.environ.get("MODEL_DIR", "models"))
    ap.add_argument("--day", type=int, default=None)
    a = ap.parse_args(argv)
    con = duckdb.connect()
    day = a.day if a.day is not None else con.sql(
        f"select max(day) from read_parquet('{a.data}/daily/*.parquet')").fetchone()[0]
    mdl = load_model(os.path.join(a.models, "gbm-v1.pkl"))
    f = load_features(a.data, day, day, con)
    p = mdl["model"].predict_proba(f[mdl["features"]].to_numpy(np.float32))[:, 1]
    top = explain_rows(f)
    on: date = START + timedelta(days=int(day))
    with psycopg.connect(os.environ["DATABASE_URL"]) as c:
        with c.cursor() as cur:
            cur.executemany("""INSERT INTO risk_score (vehicle_id, scored_on, p_fail_7d, model_version, top_factors)
                               VALUES (%s,%s,%s,%s,%s) ON CONFLICT (vehicle_id, scored_on) DO UPDATE
                               SET p_fail_7d = EXCLUDED.p_fail_7d, model_version = EXCLUDED.model_version,
                                   top_factors = EXCLUDED.top_factors""",
                            [(int(v) + 1, on, float(x), mdl["version"], json.dumps(t)) for v, x, t in zip(f.vid, p, top)])
        c.commit()
        c.autocommit = True
        c.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY fleet_daily_summary")
    try:
        import redis
        redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0")).incr("risk:version")
    except Exception as e:  # noqa: BLE001
        print(f"warning: cache version not bumped: {e!r}")
    print(json.dumps({"scored_on": str(on), "vehicles": len(f), "high_risk": int((p >= 0.5).sum())}))


if __name__ == "__main__":
    main()
