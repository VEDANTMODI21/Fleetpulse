"""Charts from measured results (bench/results/*.json, models/eval.json, docs/query_plans/summary.json)."""
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 160})
BLUE, GREY, RED, AMB = "#0969da", "#afb8c1", "#d1242f", "#bf8700"

ev = json.load(open("models/eval.json"))
names = ["Baseline A: severe DTC rule", "Baseline B: static thresholds", "Logistic regression", "FleetPulse GBM (all features)"]
short = ["Severe-DTC\nrule", "Static\nthresholds", "Logistic\nregression", "FleetPulse\nGBM"]
r = {x["model"]: x for x in ev["results"]}
fig, ax = plt.subplots(1, 2, figsize=(9, 3.2))
for a, key, title in ((ax[0], "pr_auc", "PR-AUC (7-day breakdown)"), (ax[1], "precision@1%", "Precision when flagging top 1%/day")):
    vals = [r[n][key] for n in names]
    bars = a.bar(short, vals, color=[GREY, GREY, GREY, BLUE])
    a.set_title(title, loc="left", fontsize=10)
    a.bar_label(bars, fmt="%.2f", fontsize=9)
    a.set_ylim(0, max(vals) * 1.2)
fig.tight_layout()
fig.savefig("docs/img/chart_model.png")

pb = json.load(open("bench/results/pipeline.json"))["end_to_end"]
s = pb["lag_series"]
fig, a = plt.subplots(figsize=(9, 2.8))
a.plot([x["sec"] for x in s], [x["rate"] for x in s], color=BLUE, label="offered rate (events/s)")
a.plot([x["sec"] for x in s], [x["lag"] for x in s], color=AMB, label="consumer lag (events)")
a.axvspan(*pb["burst_window_s"], color=RED, alpha=0.07, label="3x burst")
a.set_xlabel("seconds")
a.legend(frameon=False, fontsize=8, loc="upper right")
a.set_title(f"End-to-end run on 2 vCPU: {pb['ingest_stats']['accepted']:,} events accepted, {pb['loss']} lost", loc="left", fontsize=10)
fig.tight_layout()
fig.savefig("docs/img/chart_pipeline.png")

fig, a = plt.subplots(figsize=(6, 2.8))
cs, p95, p99, rps = [], [], [], []
for c in (8, 16, 32, 64):
    d = json.load(open(f"bench/results/api_c{c}.json"))
    cs.append(c); p95.append(d["overall_ms"]["p95"]); p99.append(d["overall_ms"]["p99"]); rps.append(d["rps"])
a.plot(cs, p95, "o-", color=BLUE, label="p95")
a.plot(cs, p99, "o-", color=AMB, label="p99")
a.axhline(200, color=BLUE, ls=":", lw=1)
a.axhline(500, color=AMB, ls=":", lw=1)
for c, v, q in zip(cs, p95, rps):
    a.annotate(f"{q:.0f} rps", (c, v), textcoords="offset points", xytext=(4, -12), fontsize=8)
a.set_xscale("log", base=2); a.set_xticks(cs, [str(c) for c in cs]); a.set_yscale("log")
a.set_xlabel("concurrent clients (single 2-vCPU host, load generator co-located)")
a.set_ylabel("ms"); a.legend(frameon=False, fontsize=8)
a.set_title("API latency vs concurrency (targets dotted)", loc="left", fontsize=10)
fig.tight_layout()
fig.savefig("docs/img/chart_api.png")

q = json.load(open("docs/query_plans/summary.json"))
fig, a = plt.subplots(figsize=(6, 2.6))
labels = ["Q1 top-risk list", "Q2 alert page 400", "Q3 fleet trend"]
b = [q[k]["before_ms"] for k in q]; af = [q[k]["after_ms"] for k in q]
import numpy as np
x = np.arange(3)
a.barh(x + 0.2, b, 0.4, color=GREY, label="before"); a.barh(x - 0.2, af, 0.4, color=BLUE, label="after")
a.set_yticks(x, labels); a.set_xscale("log"); a.set_xlabel("execution time, ms (log)")
a.legend(frameon=False, fontsize=8); a.set_title("EXPLAIN ANALYZE before / after", loc="left", fontsize=10)
fig.tight_layout()
fig.savefig("docs/img/chart_sql.png")
print("charts ok")
