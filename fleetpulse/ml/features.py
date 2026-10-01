"""Batch feature engineering over the Parquet lake (DuckDB SQL, window functions).

Label: breakdown within the next 7 days (days d+1 .. d+7).
Leakage controls:
  * features only use rows <= d (window frames end at CURRENT ROW);
  * truth_mode / off_road / future rows are never features;
  * train/test split is temporal with a 7-day gap (label horizon).
"""
from __future__ import annotations

import duckdb

SIGNALS = ["coolant_max", "aux_v_min", "oil_kpa_min", "misfire_cnt", "soc_drop_per100", "cell_temp_spread"]

FEATURES = (
    [f"{s}_{a}" for s in SIGNALS for a in ("last", "max3", "min3", "mean7", "slope7", "z")]
    + ["sev_dtc_3d", "sev_dtc_7d", "dtc_7d", "km_7d", "harsh_7d", "idle_ratio_7d", "days_reporting_7d",
       "age_years", "odo_km", "is_ev", "is_hybrid", "oem_beta", "oem_gamma"]
)


def feature_sql(daily_glob: str, vehicles_path: str, day_lo: int, day_hi: int, horizon: int = 7) -> str:
    win = "PARTITION BY vid ORDER BY day"
    per_sig = []
    for s in SIGNALS:
        per_sig.append(f"""
          last_value({s} IGNORE NULLS) OVER ({win} ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS {s}_last,
          max({s}) OVER ({win} ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS {s}_max3,
          min({s}) OVER ({win} ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS {s}_min3,
          avg({s}) OVER ({win} ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS {s}_mean7,
          regr_slope({s}, day) OVER ({win} ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS {s}_slope7,
          ({s} - avg({s}) OVER ({win} ROWS BETWEEN 29 PRECEDING AND 8 PRECEDING))
            / nullif(stddev_samp({s}) OVER ({win} ROWS BETWEEN 29 PRECEDING AND 8 PRECEDING), 0) AS {s}_z""")
    return f"""
    WITH d AS (SELECT * FROM read_parquet('{daily_glob}') WHERE day BETWEEN {day_lo - 30} AND {day_hi + horizon}),
    f AS (
      SELECT vid, day, off_road, {",".join(per_sig)},
        sum(severe_dtc_cnt) OVER ({win} ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS sev_dtc_3d,
        sum(severe_dtc_cnt) OVER ({win} ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS sev_dtc_7d,
        sum(dtc_cnt) OVER ({win} ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS dtc_7d,
        sum(km) OVER ({win} ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS km_7d,
        sum(harsh_brakes) OVER ({win} ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS harsh_7d,
        sum(idle_hours) OVER ({win} ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
          / nullif(sum(engine_hours) OVER ({win} ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 0) AS idle_ratio_7d,
        count(km) OVER ({win} ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS days_reporting_7d,
        max(breakdown) OVER ({win} ROWS BETWEEN 1 FOLLOWING AND {horizon} FOLLOWING) AS y,
        max(truth_mode) OVER ({win} ROWS BETWEEN CURRENT ROW AND CURRENT ROW) AS truth_mode
      FROM d)
    SELECT f.*, 2026 - v.model_year AS age_years, v.odo_km,
           (v.powertrain = 'EV')::INT AS is_ev, (v.powertrain = 'HYBRID')::INT AS is_hybrid,
           (v.oem = 'beta')::INT AS oem_beta, (v.oem = 'gamma')::INT AS oem_gamma, v.fleet_id
    FROM f JOIN read_parquet('{vehicles_path}') v USING (vid)
    WHERE f.day BETWEEN {day_lo} AND {day_hi} AND f.off_road = 0
    """


def load_features(data_dir: str, day_lo: int, day_hi: int, con=None):
    con = con or duckdb.connect()
    sql = feature_sql(f"{data_dir}/daily/*.parquet", f"{data_dir}/vehicles.parquet", day_lo, day_hi)
    return con.sql(sql).df()
