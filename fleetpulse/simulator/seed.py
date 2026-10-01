"""CLI: generate the seeded 100K-vehicle dataset (fleet registry + 90 days of rollups)."""
from __future__ import annotations

import argparse
import os
import time

import pyarrow.parquet as pq

from fleetpulse.simulator.fleet import generate_fleet
from fleetpulse.simulator.history import fleet_table, simulate_history


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicles", type=int, default=100_000)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default=os.environ.get("DATA_DIR", "data"))
    a = ap.parse_args(argv)
    t0 = time.time()
    fleet = generate_fleet(a.vehicles)
    os.makedirs(a.out, exist_ok=True)
    pq.write_table(fleet_table(fleet), os.path.join(a.out, "vehicles.parquet"))
    simulate_history(fleet, days=a.days, out_dir=os.path.join(a.out, "daily"))
    print(f"seeded {a.vehicles} vehicles x {a.days} days in {time.time() - t0:.1f}s -> {a.out}")


if __name__ == "__main__":
    main()
