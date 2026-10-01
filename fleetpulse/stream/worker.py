"""Stream worker process: owns a set of partitions of `telemetry.canonical`.

Sinks
* Redis   hash  veh:{vin}  - latest state (hot, AP)          -> dashboard / API live view
* Redis   pubsub alerts     - fan-out to SSE clients
* Postgres alert            - durable, idempotent (dedup_key)  -> CP
* Parquet lake              - micro-batched raw canonical events (warm tier, 1 file / partition / flush)
"""
from __future__ import annotations

import os
import signal
import time
from datetime import UTC, datetime

import orjson
import pyarrow as pa
import pyarrow.parquet as pq

from fleetpulse.domain.models import Alert
from fleetpulse.ingest.bus import EventBus, bus_from_env
from fleetpulse.ingest.pipeline import CANONICAL_TOPIC, decode
from fleetpulse.stream.processor import StreamProcessor

LAKE_FIELDS = ["vin", "ts", "seq", "oem", "lat", "lon", "speed_kmh", "odo_km", "coolant_c", "battery_v", "soc_pct",
               "oil_pressure_kpa", "rpm", "evt"]


class StreamWorker:
    def __init__(self, bus: EventBus, partitions: list[int], redis_client=None, pg_pool=None,
                 lake_dir: str | None = None, flush_s: float = 1.0):
        self.bus, self.parts = bus, partitions
        self.r, self.pool, self.lake_dir, self.flush_s = redis_client, pg_pool, lake_dir, flush_s
        self.pending_alerts: list[Alert] = []
        self.proc = StreamProcessor(self.pending_alerts.append)
        self.lake_buf: list = []
        self.dirty: set[str] = set()
        self.last_flush = time.time()
        self.running = True

    def step(self, max_n: int = 2000) -> int:
        n = 0
        for p in self.parts:
            for _, v in self.bus.poll(CANONICAL_TOPIC, p, max_n):
                ev = decode(v)
                self.proc.process(ev)
                self.dirty.add(ev.vin)
                if self.lake_dir:
                    self.lake_buf.append(ev)
                n += 1
        if self.pending_alerts:
            self.flush_alerts()       # alerts are flushed immediately (latency-critical path)
        if time.time() - self.last_flush >= self.flush_s:
            self.flush_state()
            # commit only after alerts are durable and the lake file is written -> at-least-once end to end
            self.bus.commit(CANONICAL_TOPIC)
        return n

    def flush_alerts(self):
        alerts, self.pending_alerts[:] = list(self.pending_alerts), []
        if self.pool is not None:
            from fleetpulse.storage.repo import Repo
            repo = Repo(self.pool)
            with self.pool.connection() as conn:
                for a in alerts:
                    ts = datetime.fromtimestamp(a.ts, UTC)
                    row = repo.insert_alert(conn, a.vin, a.kind, a.severity.value, a.detail, a.value, ts,
                                            f"{a.vin}:{a.kind}:{int(a.ts // 600)}")
                    if row:
                        a.fleet_id = row[1]
        if self.r is not None:
            pipe = self.r.pipeline(transaction=False)
            for a in alerts:
                pipe.publish("alerts", orjson.dumps({"vin": a.vin, "ts": a.ts, "kind": a.kind, "fleet_id": a.fleet_id,
                                                     "severity": a.severity.value, "detail": a.detail,
                                                     "emitted": time.time()}))
            pipe.execute()

    def flush_state(self):
        if self.r is not None and self.dirty:
            pipe = self.r.pipeline(transaction=False)
            for vin in self.dirty:
                pipe.set(f"veh:{vin}", orjson.dumps(self.proc.snapshot(vin)), ex=3600)
            pipe.set("stats:topdtc", orjson.dumps(self.proc.top_dtcs.items()))
            pipe.execute()
        if self.r is not None:
            lat = self.proc.latencies[-500:]
            self.r.hset(f"stats:worker:{self.parts[0]}", mapping={
                **{k: v for k, v in self.proc.stats.items()}, "ts": time.time(), "partitions": len(self.parts),
                "vehicles": len(self.proc.state),
                "alert_latency_p95_ms": round(sorted(lat)[int(len(lat) * 0.95)] * 1000, 1) if lat else 0})
        self.dirty.clear()
        if self.lake_dir and self.lake_buf:
            os.makedirs(self.lake_dir, exist_ok=True)
            cols = {f: [getattr(e, f) for e in self.lake_buf] for f in LAKE_FIELDS}
            cols["dtcs"] = [",".join(e.dtcs) for e in self.lake_buf]
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            path = os.path.join(self.lake_dir, f"dt={day}")
            os.makedirs(path, exist_ok=True)
            pq.write_table(pa.table(cols), os.path.join(path, f"p{self.parts[0]}-{time.time_ns()}.parquet"),
                           compression="zstd")
            self.lake_buf = []
        self.last_flush = time.time()

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        while self.running:
            if self.step() == 0:
                time.sleep(0.02)
        self.flush_state()   # graceful shutdown: drain buffers
        self.bus.commit(CANONICAL_TOPIC)


def main():  # pragma: no cover
    import redis
    from psycopg_pool import ConnectionPool
    bus = bus_from_env()
    idx, replicas = int(os.environ.get("WORKER_INDEX", "0")), int(os.environ.get("WORKER_REPLICAS", "1"))
    parts = [p for p in range(bus.partitions) if p % replicas == idx]
    pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=4)
    StreamWorker(bus, parts, redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0")),
                 pool, os.environ.get("LAKE_DIR", "data/lake")).run()


if __name__ == "__main__":
    main()
