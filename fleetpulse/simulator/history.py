"""Historical daily-rollup generator with latent degradation processes.

Each vehicle can develop a latent fault (cooling, 12V battery, misfire, oil,
HV battery). A fault progresses over 3-30 days; sensor signals drift with
progress^2 and mode-specific DTCs appear only probabilistically, late. If the
fault is not serviced the vehicle breaks down. ~10% of faults are sudden
(no precursor) to keep the problem honest. Benign/false DTCs, sensor noise,
hot/cold days and telemetry gaps are injected.

Output = one row per vehicle-day (what the stream layer's daily rollup job
produces), written as Parquet partitioned by day.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from fleetpulse.simulator.fleet import FAULT_MODES, MODES_BY_PT, Fleet

SEVERE_DTC = {"cooling": ["P0217", "P0128"], "aux_battery": ["P0562"], "misfire": ["P0300", "P0301", "P0302"],
              "oil": ["P0520", "P0524"], "hv_battery": ["P0A80", "P0AFA"]}
BENIGN_DTC = ["P0420", "P0455", "P0171", "B1000", "U0100", "P0442"]
GLITCH_DTC = ["P0118", "P0562", "P0301"]   # intermittent sensor glitches (false alarms)


def simulate_history(fleet: Fleet, days: int = 90, start: date = date(2026, 6, 1), seed: int = 11,
                     out_dir: str | None = None, base_hazard: float = 1 / 2400):
    rng = np.random.default_rng(seed)
    n = len(fleet)
    age = (2026 - fleet.model_year).astype(float)
    is_ice = fleet.powertrain != "EV"
    is_elec = fleet.powertrain != "ICE"
    mode_idx = {m: i for i, m in enumerate(FAULT_MODES)}
    allowed = np.zeros((n, len(FAULT_MODES)), bool)
    for pt, modes in MODES_BY_PT.items():
        mask = fleet.powertrain == pt
        for m in modes:
            allowed[mask, mode_idx[m]] = True

    fault = np.full(n, -1)           # active fault mode idx
    f_start = np.zeros(n)
    f_len = np.ones(n)
    f_thr = np.ones(n)
    down_until = np.full(n, -1)       # off-road until day (after breakdown)
    hazard = base_hazard * (1 + 0.12 * age) * (1 + fleet.odo_km / 400_000) * (1 + fleet.driver_aggr)

    tables = []
    for d in range(days):
        # --- fault onset
        can = (fault < 0) & (down_until < d)
        onset = can & (rng.random(n) < hazard)
        if onset.any():
            idx = np.where(onset)[0]
            probs = allowed[idx].astype(float)
            probs /= probs.sum(1, keepdims=True)
            cum = probs.cumsum(1)
            r = rng.random(len(idx))[:, None]
            fault[idx] = (r > cum).sum(1)
            f_start[idx] = d
            sudden = rng.random(len(idx)) < 0.10
            f_len[idx] = np.where(sudden, 1, rng.integers(3, 31, len(idx)))
            f_thr[idx] = rng.uniform(0.85, 1.15, len(idx))
        p = np.where(fault >= 0, np.clip((d - f_start) / f_len, 0, 1.2), 0.0)
        sudden_mask = f_len <= 1
        p_sig = np.where(sudden_mask, 0.0, p) ** 2   # sudden faults give no precursor

        def on(m, p_sig=p_sig):
            return (fault == mode_idx[m]) * p_sig

        off = down_until >= d
        gap = rng.random(n) < 0.03                 # telemetry missing
        active = ~off
        km = np.where(active, fleet.daily_km * rng.uniform(0.3, 1.4, n) * (rng.random(n) > 0.12), 0.0)
        hot_day = rng.random(n) < 0.05
        coolant = np.where(is_ice, 91 + rng.normal(0, 3, n) + 4 * fleet.driver_aggr + hot_day * rng.uniform(5, 14, n)
                           + 32 * on("cooling"), np.nan)
        aux_v = 12.35 + rng.normal(0, 0.15, n) - 0.035 * age - 1.9 * on("aux_battery") - (rng.random(n) < 0.03) * 0.5
        misfire = np.where(is_ice, rng.poisson(0.3 + 45 * on("misfire")), 0).astype(float)
        oil = np.where(is_ice & (fleet.powertrain == "ICE"), 185 + rng.normal(0, 15, n) - 115 * on("oil"), np.nan)
        soc_drop = np.where(is_elec, 17 + rng.normal(0, 1.5, n) + 3 * fleet.driver_aggr + 11 * on("hv_battery"), np.nan)
        cell_spread = np.where(is_elec, 2 + np.abs(rng.normal(0, 0.6, n)) + 9 * on("hv_battery"), np.nan)
        harsh = rng.poisson(fleet.driver_aggr * 6 * (km > 0))
        eng_hours = km / rng.uniform(22, 38, n)
        idle = eng_hours * rng.uniform(0.05, 0.35, n)

        # --- DTCs (severe appear late & probabilistically; benign + glitches anywhere)
        dtc_lists = [[] for _ in range(n)]
        sev_p = np.where(sudden_mask, 0, 0.55 * np.clip(p, 0, 1) ** 1.5)
        sev_hit = np.where((fault >= 0) & active & (rng.random(n) < sev_p))[0]
        for i in sev_hit:
            dtc_lists[i].append(rng.choice(SEVERE_DTC[FAULT_MODES[fault[i]]]))
        for i in np.where(active & (rng.random(n) < 0.035))[0]:
            dtc_lists[i].append(rng.choice(BENIGN_DTC))
        for i in np.where(active & (rng.random(n) < 0.004))[0]:
            dtc_lists[i].append(rng.choice(GLITCH_DTC))
        severe_prefix = ("P0217", "P0128", "P0562", "P030", "P0520", "P0524", "P0A", "P0118")
        severe_cnt = np.array([sum(c.startswith(severe_prefix) for c in l) for l in dtc_lists])
        dtc_cnt = np.array([len(l) for l in dtc_lists])

        # --- breakdown
        brk = (fault >= 0) & (p >= f_thr) & active
        truth = np.where(fault >= 0, fault, -1).copy()
        if brk.any():
            down_until[brk] = d + rng.integers(1, 4, brk.sum())
            fault[brk] = -1
        # random preventive repairs (workshop visits) clear some faults
        fixed = (fault >= 0) & (rng.random(n) < 0.01)
        fault[fixed] = -1

        lost = gap | off
        def nanify(a, lost=lost):  # NaN -> Parquet NULL (missing telemetry is NULL, not NaN)
            return pa.array(np.where(lost, np.nan, a).astype(np.float32), from_pandas=True)
        t = pa.table({
            "vid": np.arange(n, dtype=np.int32),
            "day": np.full(n, d, np.int16),
            "date": pa.array([start + timedelta(days=d)] * n, pa.date32()),
            "km": nanify(km),
            "engine_hours": nanify(eng_hours),
            "idle_hours": nanify(idle),
            "harsh_brakes": nanify(harsh.astype(float)),
            "coolant_max": nanify(coolant),
            "aux_v_min": nanify(aux_v),
            "oil_kpa_min": nanify(oil),
            "misfire_cnt": nanify(misfire),
            "soc_drop_per100": nanify(soc_drop),
            "cell_temp_spread": nanify(cell_spread),
            "dtc_cnt": np.where(lost, 0, dtc_cnt).astype(np.int16),
            "severe_dtc_cnt": np.where(lost, 0, severe_cnt).astype(np.int16),
            "dtcs": [",".join(l) if not lost[i] else "" for i, l in enumerate(dtc_lists)],
            "breakdown": brk.astype(np.int8),
            "truth_mode": truth.astype(np.int8),   # ground truth, evaluation only - never a feature
            "off_road": off.astype(np.int8),
        })
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            pq.write_table(t, os.path.join(out_dir, f"day={d:03d}.parquet"), compression="zstd")
        else:
            tables.append(t)
    return None if out_dir else pa.concat_tables(tables)


def fleet_table(fleet: Fleet) -> pa.Table:
    return pa.table({"vid": np.arange(len(fleet), dtype=np.int32), "vin": fleet.vin, "oem": fleet.oem,
                     "powertrain": fleet.powertrain, "model_year": fleet.model_year.astype(np.int16),
                     "odo_km": fleet.odo_km, "daily_km": fleet.daily_km, "city": fleet.city,
                     "lat": fleet.lat, "lon": fleet.lon, "fleet_id": fleet.fleet_id.astype(np.int32),
                     "driver_aggr": fleet.driver_aggr})
