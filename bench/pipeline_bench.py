"""Pipeline throughput & latency benchmark.

A. single-core component throughput (ingest: parse+validate+dedup+encode; stream: decode+state+rules)
B. process scaling of ingest (1 vs N processes, shared-nothing, partitioned by VIN)
C. end-to-end on Redis Streams: simulator -> ingest -> bus -> 2 stream workers -> Redis/Postgres,
   sustained rate with a 3x burst, sampling consumer lag and event->alert latency.
Writes bench/results/pipeline.json
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import subprocess
import sys
import threading
import time

import numpy as np
import orjson
import redis

from fleetpulse.ingest.bus import InMemoryBus, RedisStreamsBus
from fleetpulse.ingest.pipeline import CANONICAL_TOPIC, IngestPipeline, decode
from fleetpulse.simulator.run import load_fleet
from fleetpulse.simulator.stream import StreamSimulator
from fleetpulse.stream.processor import StreamProcessor

DATA = os.environ.get("DATA_DIR", "data")


def gen_events(n_ticks=2, n=100_000):
    fleet = load_fleet(f"{DATA}/vehicles.parquet")
    sim = StreamSimulator(fleet)
    out = []
    t = time.time() - 10
    for k in range(n_ticks):
        out += sim.tick(t + k, np.arange(n))
    return out


def ingest_worker(events, q):
    bus = InMemoryBus(8)
    pipe = IngestPipeline(bus)
    t = time.perf_counter()
    for o, p in events:
        pipe.offer(o, p)
    pipe.flush()
    q.put((len(events), time.perf_counter() - t))


def pct(xs, p):
    return float(np.percentile(xs, p)) if xs else None


def main(rate=int(os.environ.get("E2E_RATE", "12000")), seconds=int(os.environ.get("E2E_SECONDS", "90"))):
    res = {"host": {"cpus": os.cpu_count()}}
    events = gen_events()
    # ---------------- A
    bus = InMemoryBus(8)
    pipe = IngestPipeline(bus)
    t = time.perf_counter()
    for o, p in events:
        pipe.offer(o, p)
    pipe.flush()
    ing_s = time.perf_counter() - t
    sp = StreamProcessor(lambda a: None)
    msgs = [v for p in range(8) for _, v in bus.poll(CANONICAL_TOPIC, p, 10**7)]
    t = time.perf_counter()
    for v in msgs:
        sp.process(decode(v))
    st_s = time.perf_counter() - t
    res["single_core"] = {"events": len(events), "ingest_eps": int(len(events) / ing_s),
                          "stream_eps": int(len(msgs) / st_s), "ingest_stats": pipe.stats}
    # ---------------- B
    scale = {}
    for procs in (1, 2):
        q = mp.Queue()
        chunks = [events[i::procs] for i in range(procs)]
        ps = [mp.Process(target=ingest_worker, args=(c, q)) for c in chunks]
        t = time.perf_counter()
        [p.start() for p in ps]
        [p.join() for p in ps]
        wall = time.perf_counter() - t
        scale[procs] = int(len(events) / wall)
    res["ingest_process_scaling_eps"] = scale
    print(json.dumps(res, indent=2), flush=True)

    # ---------------- C  end-to-end
    r = redis.Redis()
    r.flushall()
    env = {**os.environ, "BUS": "redis", "PARTITIONS": "8", "WORKER_REPLICAS": "2", "LAKE_DIR": "/tmp/lake-bench"}
    workers = [subprocess.Popen([sys.executable, "-m", "fleetpulse.stream.worker"], env={**env, "WORKER_INDEX": str(i)},
                                stdout=subprocess.DEVNULL, stderr=open(f"/tmp/w{i}.log", "w")) for i in range(2)]
    time.sleep(2)
    lat_alert, lat_recv = [], []
    stop = threading.Event()

    def listen():
        ps = r.pubsub()
        ps.subscribe("alerts")
        while not stop.is_set():
            m = ps.get_message(timeout=0.2)
            if m and m["type"] == "message":
                a = orjson.loads(m["data"])
                lat_alert.append(a["emitted"] - a["ts"])
                lat_recv.append(time.time() - a["ts"])
    th = threading.Thread(target=listen, daemon=True)
    th.start()

    fleet = load_fleet(f"{DATA}/vehicles.parquet")
    sim = StreamSimulator(fleet)
    rng = np.random.default_rng(3)
    degr = rng.choice(len(fleet), 300, replace=False)
    for i in degr:
        sim.degrade_now(int(i), 0.05)        # these produce DTC / BREAKDOWN alerts during the run
    rbus = RedisStreamsBus(partitions=8)
    ipipe = IngestPipeline(rbus, flush_size=5000)
    lag_samples, sent_total, cursor = [], 0, 0
    burst = (seconds // 3, seconds // 3 + 20)
    t_start = time.time()
    for sec in range(seconds):
        t0 = time.time()
        rr = rate * (3 if burst[0] <= sec < burst[1] else 1)
        idx = np.unique(np.concatenate([np.arange(cursor, cursor + rr) % len(fleet), degr]))
        cursor = (cursor + rr) % len(fleet)
        for o, p in sim.tick(time.time(), idx):
            ipipe.offer(o, p)
        ipipe.flush()
        sent_total += len(idx)
        lag = rbus.lag(CANONICAL_TOPIC)
        lag_samples.append({"sec": sec, "rate": rr, "lag": lag, "gen_ingest_s": round(time.time() - t0, 3)})
        dt = time.time() - t0
        if dt < 1:
            time.sleep(1 - dt)
    # drain
    t_drain = time.time()
    while rbus.lag(CANONICAL_TOPIC) > 0 and time.time() - t_drain < 120:
        time.sleep(0.5)
    drain_s = time.time() - t_drain
    time.sleep(1.5)
    stop.set()
    for w in workers:
        w.terminate()
        w.wait()
    wall = time.time() - t_start
    # count what reached the workers: lake rows
    import duckdb
    lake_rows = duckdb.sql("select count(*) from read_parquet('/tmp/lake-bench/*/*.parquet')").fetchone()[0]
    res["end_to_end"] = {
        "target_rate_eps": rate, "burst_rate_eps": rate * 3, "burst_window_s": list(burst), "seconds": seconds,
        "raw_events_sent": sent_total, "ingest_stats": ipipe.stats, "events_in_lake": int(lake_rows),
        "loss": int(ipipe.stats["accepted"] - lake_rows), "drain_after_run_s": round(drain_s, 1),
        "max_lag": max(s["lag"] for s in lag_samples), "lag_p95": pct([s["lag"] for s in lag_samples], 95),
        "alerts": len(lat_alert), "event_to_alert_ms": {k: round(pct(lat_alert, p) * 1000, 1) for k, p in
                                                        (("p50", 50), ("p95", 95), ("p99", 99))} if lat_alert else None,
        "event_to_subscriber_ms": {k: round(pct(lat_recv, p) * 1000, 1) for k, p in
                                   (("p50", 50), ("p95", 95), ("p99", 99))} if lat_recv else None,
        "wall_s": round(wall, 1), "lag_series": lag_samples,
    }
    os.makedirs("bench/results", exist_ok=True)
    json.dump(res, open("bench/results/pipeline.json", "w"), indent=2)
    print(json.dumps({k: v for k, v in res["end_to_end"].items() if k != "lag_series"}, indent=2))


if __name__ == "__main__":
    main()
