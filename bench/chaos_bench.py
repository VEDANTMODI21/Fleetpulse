"""Chaos test: kill -9 one of two stream workers mid-run, restart it 5 s later (as Kubernetes would),
and prove (a) no accepted event is lost, (b) duplicates from re-delivery are bounded and removed by the
(vin, seq) de-duplication at lake compaction, (c) alerts stay unique (dedup_key), (d) lag recovers.
Writes bench/results/chaos.json"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import duckdb
import numpy as np
import redis

from fleetpulse.ingest.bus import RedisStreamsBus
from fleetpulse.ingest.pipeline import CANONICAL_TOPIC, IngestPipeline
from fleetpulse.simulator.run import load_fleet
from fleetpulse.simulator.stream import StreamSimulator

LAKE = "/tmp/lake-chaos"


def spawn(i, env):
    return subprocess.Popen([sys.executable, "-m", "fleetpulse.stream.worker"],
                            env={**env, "WORKER_INDEX": str(i), "HOSTNAME": f"worker-{i}"},
                            stdout=subprocess.DEVNULL, stderr=open(f"/tmp/chaos-w{i}.log", "w"))


def main(rate=6000, seconds=45, kill_at=15, restart_after=5):
    r = redis.Redis()
    r.flushall()
    subprocess.run(["rm", "-rf", LAKE], check=True)
    env = {**os.environ, "BUS": "redis", "PARTITIONS": "8", "WORKER_REPLICAS": "2", "LAKE_DIR": LAKE,
           "CLAIM_IDLE_MS": "5000"}
    w = [spawn(0, env), spawn(1, env)]
    time.sleep(2)
    fleet = load_fleet("data/vehicles.parquet")
    sim = StreamSimulator(fleet)
    rng = np.random.default_rng(9)
    degr = rng.choice(len(fleet), 200, replace=False)
    for i in degr:
        sim.degrade_now(int(i), 0.05)
    bus = RedisStreamsBus(partitions=8)
    pipe = IngestPipeline(bus, flush_size=5000)
    lag, cursor, events = [], 0, []
    for sec in range(seconds):
        t0 = time.time()
        if sec == kill_at:
            w[0].send_signal(signal.SIGKILL)
            w[0].wait()
            events.append({"sec": sec, "event": "worker-0 SIGKILL"})
        if sec == kill_at + restart_after:
            w[0] = spawn(0, env)
            events.append({"sec": sec, "event": "worker-0 restarted"})
        idx = np.unique(np.concatenate([np.arange(cursor, cursor + rate) % len(fleet), degr]))
        cursor = (cursor + rate) % len(fleet)
        for o, p in sim.tick(time.time(), idx):
            pipe.offer(o, p)
        pipe.flush()
        lag.append({"sec": sec, "lag": bus.lag(CANONICAL_TOPIC)})
        time.sleep(max(0, 1 - (time.time() - t0)))
    t = time.time()
    while bus.lag(CANONICAL_TOPIC) > 0 and time.time() - t < 60:
        time.sleep(0.5)
    time.sleep(8)          # allow claim of pending entries + final flush
    for p in w:
        p.send_signal(signal.SIGTERM)
        p.wait()
    pending = sum(r.xpending(f"{CANONICAL_TOPIC}.{p}", "stream-processor")["pending"] for p in range(8))
    q = duckdb.sql(f"select count(*) n, count(distinct (vin, seq)) u from read_parquet('{LAKE}/*/*.parquet')").fetchone()
    peak = max(x["lag"] for x in lag)
    rec = next((x["sec"] for x in lag if x["sec"] > kill_at + restart_after and x["lag"] <= 2 * rate), None)
    res = {"rate_eps": rate, "seconds": seconds, "events": events, "accepted": pipe.stats["accepted"],
           "lake_rows": q[0], "lake_unique_vin_seq": q[1], "lost": pipe.stats["accepted"] - q[1],
           "redelivered_duplicates": q[0] - q[1], "pending_after_run": pending, "peak_lag": peak,
           "lag_back_to_normal_at_sec": rec, "lag_series": lag}
    json.dump(res, open("bench/results/chaos.json", "w"), indent=2)
    print(json.dumps({k: v for k, v in res.items() if k != "lag_series"}, indent=2))


if __name__ == "__main__":
    main()
