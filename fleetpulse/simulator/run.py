"""Live simulator CLI.

  python -m fleetpulse.simulator.run --rate 20000 --mode direct   # edge-gateway mode: normalise + publish to bus
  python -m fleetpulse.simulator.run --rate 5000 --mode http --target http://localhost:8001

Uses the seeded registry (data/vehicles.parquet) so VINs match the database.
`--degrade VIN` forces one vehicle into a fast overheat so a live alert appears in the demo.
"""
from __future__ import annotations

import argparse
import os
import time

import numpy as np
import pyarrow.parquet as pq

from fleetpulse.simulator.fleet import Fleet
from fleetpulse.simulator.stream import StreamConfig, StreamSimulator


def load_fleet(path: str) -> Fleet:
    t = pq.read_table(path).to_pandas()
    return Fleet(t.vin.to_numpy(), t.oem.to_numpy(), t.powertrain.to_numpy(), t.model_year.to_numpy(),
                 t.odo_km.to_numpy(), t.daily_km.to_numpy(), t.city.to_numpy(), t.lat.to_numpy(), t.lon.to_numpy(),
                 t.fleet_id.to_numpy(), t.driver_aggr.to_numpy())


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("DATA_DIR", "data"))
    ap.add_argument("--rate", type=int, default=10_000, help="events per second (fleet-wide)")
    ap.add_argument("--seconds", type=int, default=0, help="0 = run forever")
    ap.add_argument("--mode", choices=["direct", "http"], default="direct")
    ap.add_argument("--target", default="http://localhost:8001")
    ap.add_argument("--burst-at", type=int, default=-1, help="second at which a 3x burst starts (60 s)")
    ap.add_argument("--degrade", nargs="*", default=[])
    ap.add_argument("--degrade-rate", type=float, default=0.02, help="health lost per second by degraded vehicles")
    a = ap.parse_args(argv)
    fleet = load_fleet(os.path.join(a.data, "vehicles.parquet"))
    sim = StreamSimulator(fleet, StreamConfig())
    for vin in a.degrade:
        idx = int(np.where(fleet.vin == vin)[0][0])
        sim.degrade_now(idx, a.degrade_rate)
    if a.mode == "direct":
        from fleetpulse.ingest.bus import bus_from_env
        from fleetpulse.ingest.pipeline import IngestPipeline
        pipe = IngestPipeline(bus_from_env())
        send = lambda batch: [pipe.offer(o, p) for o, p in batch] and pipe.flush()  # noqa: E731
    else:
        import httpx
        import orjson
        client = httpx.Client(timeout=10)

        def send(batch):
            by_oem: dict[str, list] = {}
            for o, p in batch:
                by_oem.setdefault(o, []).append(p)
            for o, items in by_oem.items():
                for i in range(0, len(items), 2000):
                    client.post(f"{a.target}/ingest/{o}", content=orjson.dumps(items[i:i + 2000]),
                                headers={"x-api-key": os.environ[f"INGEST_KEY_{o.upper()}"],
                                         "content-type": "application/json"})
    n, cursor, t_start, sec = len(fleet), 0, time.time(), 0
    degr = np.array([int(np.where(fleet.vin == v)[0][0]) for v in a.degrade], dtype=int)
    while a.seconds == 0 or sec < a.seconds:
        t0 = time.time()
        rate = a.rate * (3 if a.burst_at >= 0 and a.burst_at <= sec < a.burst_at + 60 else 1)
        idx = (np.arange(cursor, cursor + rate) % n)
        idx = np.unique(np.concatenate([idx, degr])) if len(degr) else idx
        cursor = (cursor + rate) % n
        batch = sim.tick(time.time(), idx)
        send(batch)
        sec += 1
        dt = time.time() - t0
        print(f"t={sec}s sent={len(batch)} in {dt:.2f}s", flush=True)
        if dt < 1:
            time.sleep(1 - dt)
    print(f"done in {time.time() - t_start:.1f}s")


if __name__ == "__main__":
    main()
