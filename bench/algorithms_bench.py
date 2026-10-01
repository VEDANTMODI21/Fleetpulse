"""Measured runtime / accuracy of the core algorithms at scale. Writes bench/results/algorithms.json."""
from __future__ import annotations

import json
import os
import random
import time

from fleetpulse.algorithms.dtc import parse_dtcs
from fleetpulse.algorithms.planning import Candidate, grid_graph, nearest_depot, plan_greedy, plan_service_slots
from fleetpulse.algorithms.streaming import BloomFilter, CountMinSketch, SlidingWindow, TopK, geohash_encode
from fleetpulse.algorithms.vin import is_valid_vin, make_vin


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t


def main():
    rng = random.Random(1)
    res = {}
    vins = [make_vin(rng) for _ in range(200_000)]
    bad = [v[:8] + ("0" if v[8] != "0" else "1") + v[9:] for v in vins[:100_000]]
    ok, dt = timed(lambda: sum(is_valid_vin(v) for v in vins))
    rej, _ = timed(lambda: sum(not is_valid_vin(v) for v in bad))
    res["vin_validate"] = {"n": len(vins), "seconds": round(dt, 3), "per_sec": int(len(vins) / dt),
                           "valid_accepted": ok, "bad_checkdigit_rejected": rej, "bad_n": len(bad)}

    payloads = ["DTC=P0301;P0420", "0x0301 0x0420", ["p0217", "P0A80"], "codes: C1234 U0100 junk", ""] * 40_000
    _, dt = timed(lambda: [parse_dtcs(p) for p in payloads])
    res["dtc_parse"] = {"n": len(payloads), "seconds": round(dt, 3), "per_sec": int(len(payloads) / dt)}

    n = 1_000_000
    bf = BloomFilter(n, 1e-3)
    _, dt = timed(lambda: [bf.add(f"k{i}".encode()) for i in range(n)])
    fp = sum(f"x{i}".encode() in bf for i in range(200_000)) / 200_000
    res["bloom"] = {"n": n, "bits_MB": round(bf.m / 8 / 1e6, 2), "k": bf.k, "insert_per_sec": int(n / dt),
                    "measured_fp_rate": fp, "target_fp_rate": 1e-3}

    top = TopK(10, CountMinSketch(2048, 5))
    codes = [f"P{rng.randint(0, 9999):04d}" for _ in range(2000)]
    weights = [1 / (i + 1) ** 1.1 for i in range(len(codes))]
    stream = rng.choices(codes, weights, k=500_000)
    true = {}
    for c in stream:
        true[c] = true.get(c, 0) + 1
    _, dt = timed(lambda: [top.add(c) for c in stream])
    exact_top = sorted(true, key=true.get, reverse=True)[:10]
    got = [k for k, _ in top.items()]
    over = [top.cms.estimate(c) - true[c] for c in codes[:200]]
    res["count_min_topk"] = {"n": len(stream), "per_sec": int(len(stream) / dt), "top10_overlap": len(set(got) & set(exact_top)),
                             "mean_overestimate": round(sum(over) / len(over), 2), "memory_counters": 2048 * 5}

    w = SlidingWindow(900)
    _, dt = timed(lambda: [w.add(i * 1.0, (i * 37) % 101) for i in range(1_000_000)])
    res["sliding_window"] = {"n": 1_000_000, "per_sec": int(1_000_000 / dt)}

    _, dt = timed(lambda: [geohash_encode(12.97 + i * 1e-6, 77.59, 6) for i in range(200_000)])
    res["geohash"] = {"n": 200_000, "per_sec": int(200_000 / dt)}

    cands = [Candidate(f"V{i}", rng.random() ** 2, rng.choice([2200, 1830, 2500, 4700, 6700]),
                       rng.choice([180, 300, 450, 600, 1500]), rng.choice([0.5, 1.5, 2.5, 3.0, 4.0])) for i in range(500)]
    (chosen, val), dt = timed(lambda: plan_service_slots(cands, 30))
    (g_ch, g_val), _ = timed(lambda: plan_greedy(cands, 30))
    res["knapsack_dp"] = {"n": 500, "budget_hours": 30, "positive_value_items": sum(c.value > 0 for c in cands), "seconds": round(dt, 3), "dp_value": round(val, 1),
                          "greedy_value": round(g_val, 1), "dp_gain_pct": round((val - g_val) / g_val * 100, 2)}

    g = grid_graph(300)
    depots = {f"{rng.randrange(300)},{rng.randrange(300)}": 3 for _ in range(40)}
    t = time.perf_counter()
    for _ in range(20):
        nearest_depot(g, f"{rng.randrange(300)},{rng.randrange(300)}", depots)
    res["dijkstra"] = {"nodes": 90_000, "edges": sum(len(v) for v in g.values()), "depots": len(depots),
                       "ms_per_query": round((time.perf_counter() - t) / 20 * 1000, 1)}
    os.makedirs("bench/results", exist_ok=True)
    json.dump(res, open("bench/results/algorithms.json", "w"), indent=2)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
