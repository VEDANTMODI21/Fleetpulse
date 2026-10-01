import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from fleetpulse.algorithms.vin import is_valid_vin
from fleetpulse.ml.features import FEATURES, load_features
from fleetpulse.ml.train import at_budget, rule_dtc, rule_thresholds
from fleetpulse.simulator.fleet import generate_fleet
from fleetpulse.simulator.history import fleet_table, simulate_history


def test_fleet_generation():
    f = generate_fleet(3000, seed=1)
    assert len(f) == 3000 and len(set(f.vin)) == 3000 and all(is_valid_vin(v) for v in f.vin[:500])
    assert set(f.powertrain) == {"ICE", "HYBRID", "EV"} and f.fleet_id.min() >= 1


def test_history_and_features_no_leakage(tmp_path):
    f = generate_fleet(1500, seed=2)
    pq.write_table(fleet_table(f), tmp_path / "vehicles.parquet")
    simulate_history(f, days=45, out_dir=str(tmp_path / "daily"), base_hazard=1 / 150)
    df = load_features(str(tmp_path), 35, 37)
    assert set(FEATURES) <= set(df.columns) and len(df) > 3000
    assert df.y.sum() > 0
    ev = df[df.is_ev == 1]
    assert ev.coolant_max_last.isna().all()                        # EVs have no coolant signal
    raw = pd.read_parquet(tmp_path / "daily")
    assert raw.coolant_max.isna().any()                             # telemetry gaps are NULL
    # label = breakdown strictly in the future: compare with raw for one positive row
    row = df[df.y == 1].iloc[0]
    fut = raw[(raw.vid == row.vid) & (raw.day > row.day) & (raw.day <= row.day + 7)]
    assert fut.breakdown.max() == 1


def test_budget_metric_and_rules():
    df = pd.DataFrame({"day": [1] * 200, "y": [1] * 2 + [0] * 198, "sev_dtc_3d": [1, 0] + [0] * 198,
                       "coolant_max_max3": [120] + [90] * 199, "aux_v_min_min3": [12.3] * 200,
                       "oil_kpa_min_min3": [180] * 200, "misfire_cnt_max3": [0] * 200,
                       "cell_temp_spread_max3": [np.nan] * 200})
    m = at_budget(df, np.array([0.9, 0.8] + [0.0] * 198))
    assert m["precision"] == 1.0 and m["recall"] == 1.0 and m["flagged"] == 2
    assert rule_dtc(df)[0] == 1 and rule_thresholds(df)[0] == 2
