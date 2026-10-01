"""Train + evaluate the 7-day breakdown model against rule baselines.

Temporal split: train days [30, 60], test days [68, 82]  (gap > 7-day horizon).
Metrics: PR-AUC, precision / recall at a fixed daily alert budget (top 1% of
fleet = workshop capacity), and lead time of caught breakdowns.
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from fleetpulse.ml.features import FEATURES, load_features
from fleetpulse.ml.registry import save_model

TRAIN = (30, 60)
TEST = (68, 82)
BUDGET = 0.01  # flag top 1% of reporting vehicles per day


def rule_dtc(df: pd.DataFrame) -> np.ndarray:
    """Baseline A - what most fleets do today: act on a severe DTC (last 3 days)."""
    return (df["sev_dtc_3d"].fillna(0) > 0).astype(float).to_numpy()


def rule_thresholds(df: pd.DataFrame) -> np.ndarray:
    """Baseline B - static OEM-style thresholds + severe DTC; score = number of rules fired."""
    r = ((df["coolant_max_max3"] > 108).astype(int) + (df["aux_v_min_min3"] < 11.6).astype(int)
         + (df["oil_kpa_min_min3"] < 110).astype(int) + (df["misfire_cnt_max3"] > 8).astype(int)
         + (df["cell_temp_spread_max3"] > 6).astype(int) + (df["sev_dtc_3d"] > 0).astype(int))
    return r.to_numpy(dtype=float)


def at_budget(df: pd.DataFrame, score: np.ndarray, budget: float = BUDGET) -> dict:
    """Per day, flag top `budget` share of vehicles; ties broken randomly (fair to rule baselines)."""
    rng = np.random.default_rng(0)
    d = pd.DataFrame({"day": df["day"].to_numpy(), "y": df["y"].to_numpy(),
                      "s": score + rng.random(len(score)) * 1e-6})
    d["rank"] = d.groupby("day")["s"].rank(ascending=False, method="first")
    d["k"] = d.groupby("day")["s"].transform("size") * budget
    flagged = (d["rank"] <= d["k"]) & (d["s"] > 1e-5)
    tp = int((flagged & (d.y == 1)).sum())
    return {"precision": tp / max(1, int(flagged.sum())), "recall": tp / max(1, int(d.y.sum())),
            "flagged": int(flagged.sum()), "tp": tp}


def evaluate(df, score, name):
    out = {"model": name, "pr_auc": float(average_precision_score(df["y"], score)),
           "roc_auc": float(roc_auc_score(df["y"], score))}
    out.update({f"{k}@1%": v for k, v in at_budget(df, score).items()})
    return out


def main(data_dir="data", model_dir="models"):
    t0 = time.time()
    tr = load_features(data_dir, *TRAIN)
    te = load_features(data_dir, *TEST)
    load_s = time.time() - t0
    # negative down-sampling for speed; weights keep calibration
    rng = np.random.default_rng(1)
    keep = (tr["y"] == 1) | (rng.random(len(tr)) < 0.25)
    trs = tr[keep]
    w = np.where(trs["y"] == 1, 1.0, 4.0)

    X, y = trs[FEATURES].to_numpy(np.float32), trs["y"].to_numpy()
    t1 = time.time()
    gbm = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08, max_leaf_nodes=48,
                                         l2_regularization=1.0, early_stopping=True, random_state=0)
    gbm.fit(X, y, sample_weight=w)
    train_s = time.time() - t1

    lr = make_pipeline(SimpleImputer(), StandardScaler(), LogisticRegression(max_iter=500))
    lr.fit(X, y, logisticregression__sample_weight=w)

    # ablation: without per-vehicle self-baseline z-score features
    no_z = [f for f in FEATURES if not f.endswith("_z")]
    gbm_noz = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08, max_leaf_nodes=48,
                                             l2_regularization=1.0, early_stopping=True, random_state=0)
    gbm_noz.fit(trs[no_z].to_numpy(np.float32), y, sample_weight=w)

    Xt = te[FEATURES].to_numpy(np.float32)
    t2 = time.time()
    p_gbm = gbm.predict_proba(Xt)[:, 1]
    infer_us = (time.time() - t2) / len(Xt) * 1e6
    results = [
        evaluate(te, rule_dtc(te), "Baseline A: severe DTC rule"),
        evaluate(te, rule_thresholds(te), "Baseline B: static thresholds"),
        evaluate(te, lr.predict_proba(Xt)[:, 1], "Logistic regression"),
        evaluate(te, gbm_noz.predict_proba(te[no_z].to_numpy(np.float32))[:, 1], "GBM without self-baseline z"),
        evaluate(te, p_gbm, "FleetPulse GBM (all features)"),
    ]
    # event-level: a breakdown (bday) is "caught" if the vehicle was flagged on any day in
    # [bday-7, bday-1]; only events whose whole 7-day look-back lies inside the test window count.
    import duckdb
    brk = duckdb.sql(f"select vid, day as bday from read_parquet('{data_dir}/daily/*.parquet') "
                     f"where breakdown=1 and day between {TEST[0] + 7} and {TEST[1] + 1}").df()
    te = te.assign(p=p_gbm)
    thr = float(np.quantile(p_gbm, 1 - BUDGET))
    ev_gbm = event_recall(te, te.p >= thr, brk)
    rb = rule_thresholds(te)
    ev_rule = event_recall(te, rb >= 1, brk)
    ev_dtc = event_recall(te, rule_dtc(te) > 0, brk)
    by_mode = (te[te.y == 1].assign(hit=te.p >= thr).groupby("truth_mode")["hit"].mean().to_dict())

    imp = feature_importance(gbm, te, FEATURES)
    meta = {
        "rows_train": int(len(tr)), "rows_train_sampled": int(len(trs)), "rows_test": int(len(te)),
        "pos_rate_test": float(te.y.mean()), "feature_build_s": round(load_s, 1), "train_s": round(train_s, 1),
        "inference_us_per_row": round(infer_us, 2), "threshold_top1pct": float(thr),
        "events": {"gbm_top1pct": ev_gbm, "thresholds_any_rule": ev_rule, "severe_dtc_rule": ev_dtc},
        "recall_by_mode": {str(k): round(float(v), 3) for k, v in by_mode.items()},
        "results": results, "top_features": imp[:12],
    }
    os.makedirs(model_dir, exist_ok=True)
    save_model({"model": gbm, "features": FEATURES, "threshold": float(thr), "version": "gbm-v1"},
               os.path.join(model_dir, "gbm-v1.pkl"))
    with open(os.path.join(model_dir, "eval.json"), "w") as fh:
        json.dump(meta, fh, indent=2)
    print(json.dumps(meta, indent=2))
    return meta


def event_recall(te, flag, brk):
    f = te.loc[np.asarray(flag), ["vid", "day"]]
    m = brk.merge(f, on="vid")
    m = m[(m.day >= m.bday - 7) & (m.day <= m.bday - 1)]
    lead = (m.bday - m.day).groupby([m.vid, m.bday]).max()
    return {"events": int(len(brk)), "caught": int(len(lead)), "recall": round(len(lead) / max(1, len(brk)), 3),
            "median_lead_days": float(np.median(lead)) if len(lead) else 0.0,
            "flags_per_day": round(float(np.asarray(flag).sum()) / te.day.nunique(), 1)}


def feature_importance(model, df, feats, n=20000):
    """Permutation importance on a stratified sample (PR-AUC drop)."""
    rng = np.random.default_rng(3)
    pos = df[df.y == 1]
    neg = df[df.y == 0].sample(min(n, (df.y == 0).sum()), random_state=3)
    s = pd.concat([pos, neg])
    X = s[feats].to_numpy(np.float32)
    base = average_precision_score(s.y, model.predict_proba(X)[:, 1])
    out = []
    for i, f in enumerate(feats):
        Xp = X.copy()
        Xp[:, i] = rng.permutation(Xp[:, i])
        out.append((f, round(float(base - average_precision_score(s.y, model.predict_proba(Xp)[:, 1])), 4)))
    return sorted(out, key=lambda kv: -kv[1])


if __name__ == "__main__":
    main(os.environ.get("DATA_DIR", "data"), os.environ.get("MODEL_DIR", "models"))
