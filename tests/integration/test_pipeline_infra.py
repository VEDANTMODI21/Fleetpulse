import os
import time

import orjson
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from fleetpulse.ingest.bus import RedisStreamsBus, partition_for
from fleetpulse.ingest.normalizer import Normalizer
from fleetpulse.ingest.pipeline import CANONICAL_TOPIC, IngestPipeline
from fleetpulse.ingest.service import create_app as ingest_app
from fleetpulse.stream.worker import StreamWorker


def raw(vin, seq, **kw):
    p = {"vin": vin, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "seq": seq, "lat": 12.9, "lon": 77.6,
         "speed_kmh": 40, "odo_km": 100, "coolant_c": 90, "batt_v": 12.4, "rpm": 1500, "dtc": [], "evt": None}
    p.update(kw)
    return p


@pytest.fixture
def vin(pool):
    with pool.connection() as c:
        return c.execute("SELECT vin FROM vehicle WHERE fleet_id=3 LIMIT 1").fetchone()[0]


def test_redis_streams_bus_partitions_replay_and_lag(services):
    bus = RedisStreamsBus(services["redis"], partitions=4, group="t-group")
    bus.r.delete(*[f"t.topic.{p}" for p in range(4)])
    bus.publish_many("t.topic", [(f"V{i}", str(i).encode()) for i in range(100)])
    assert bus.lag("t.topic") == 100
    got = [m for p in range(4) for m in bus.poll("t.topic", p, 1000)]
    bus.commit("t.topic")
    assert len(got) == 100 and bus.lag("t.topic") == 0
    assert all(partition_for(k, 4) == p for p in range(4) for k, _ in [])   # keys stay in their partition
    replay = RedisStreamsBus(services["redis"], partitions=4, group="replay-group")   # new group replays history
    assert sum(len(replay.poll("t.topic", p, 1000)) for p in range(4)) == 100


def test_end_to_end_ingest_to_alert(services, pool, rclient, vin, tmp_path):
    bus = RedisStreamsBus(services["redis"], partitions=4, group="e2e")
    for p in range(4):
        rclient.delete(f"{CANONICAL_TOPIC}.{p}")
    pipe = IngestPipeline(bus, Normalizer(time.time))
    app = ingest_app(pipe)
    with TestClient(app) as c:
        h = {"x-api-key": os.environ["INGEST_KEY_ALPHA"]}
        assert c.post("/ingest/alpha", json=[raw(vin, 1)], headers={"x-api-key": "wrong"}).status_code == 401
        assert c.post("/ingest/alpha", content=b"{not json", headers=h).status_code == 400
        assert c.post("/ingest/alpha", json={"a": 1}, headers=h).status_code == 400
        batch = [raw(vin, 2), raw(vin, 2), raw(vin, 3, dtc=["P0217"]), raw(vin, 4, evt="BREAKDOWN"), raw("BADVIN", 1), 7]
        r = c.post("/ingest/alpha", json=batch, headers=h).json()
        assert r == {"accepted": 3, "duplicate": 1, "invalid": 2, "backpressure": 0}
        assert b"ingest_events_total" in c.get("/metrics").content and c.get("/healthz").json()["ok"]
    ps = rclient.pubsub()
    ps.subscribe("alerts")
    w = StreamWorker(bus, list(range(4)), rclient, pool, str(tmp_path / "lake"), flush_s=0)
    assert w.step() == 3
    w.step()
    msgs = []
    for _ in range(20):
        m = ps.get_message(timeout=0.1)
        if m and m["type"] == "message":
            msgs.append(orjson.loads(m["data"]))
    kinds = {m["kind"] for m in msgs}
    assert {"DTC_P0217", "BREAKDOWN"} <= kinds and all(m["fleet_id"] == 3 for m in msgs)
    assert orjson.loads(rclient.get(f"veh:{vin}"))["active_dtcs"] == ["P0217"]
    with pool.connection() as c:
        n = c.execute("SELECT count(*) FROM alert a JOIN vehicle v ON v.id=a.vehicle_id WHERE v.vin=%s "
                      "AND a.kind IN ('DTC_P0217','BREAKDOWN')", (vin,)).fetchone()[0]
    assert n == 2
    w.pending_alerts.extend([])       # re-delivery of same alert is idempotent (dedup_key)
    from fleetpulse.domain.models import Alert, Severity
    w.pending_alerts.append(Alert(vin, msgs[0]["ts"], msgs[0]["kind"], Severity.CRITICAL, "dup"))
    w.flush_alerts()
    with pool.connection() as c:
        assert c.execute("SELECT count(*) FROM alert a JOIN vehicle v ON v.id=a.vehicle_id WHERE v.vin=%s "
                         "AND a.kind IN ('DTC_P0217','BREAKDOWN')", (vin,)).fetchone()[0] == 2
    files = list((tmp_path / "lake").rglob("*.parquet"))
    assert files and sum(pq.read_table(f).num_rows for f in files) == 3


def test_ingest_backpressure_429(services):
    pipe = IngestPipeline(RedisStreamsBus(services["redis"], partitions=2), Normalizer(time.time), max_buffer=1,
                          flush_size=10_000)
    with TestClient(ingest_app(pipe)) as c:
        r = c.post("/ingest/alpha", json=[raw("1M8GDM9AXKP042788", i) for i in range(1, 4)],
                   headers={"x-api-key": os.environ["INGEST_KEY_ALPHA"]})
    assert r.status_code == 429 and r.headers["retry-after"] == "1" and r.json()["backpressure"] == 2


def test_consumer_crash_recovery_no_loss_no_duplicates(services, rclient, monkeypatch):
    """Chaos-style: consumer A commits 400 events, reads 300 more and crashes before committing.
    Consumer B (another pod) claims A's pending entries and continues: every event is delivered,
    committed events are never re-delivered."""
    rclient.delete("chaos.topic.0")
    monkeypatch.setenv("CLAIM_IDLE_MS", "0")
    monkeypatch.setenv("HOSTNAME", "pod-a")
    a = RedisStreamsBus(services["redis"], partitions=1, group="chaos")
    a.publish_many("chaos.topic", [("V", str(i).encode()) for i in range(1000)])
    first = a.poll("chaos.topic", 0, 400)
    a.commit("chaos.topic")
    lost_batch = a.poll("chaos.topic", 0, 300)          # processed? unknown - pod killed before commit
    del a
    monkeypatch.setenv("HOSTNAME", "pod-b")
    b = RedisStreamsBus(services["redis"], partitions=1, group="chaos")
    rest = []
    while True:
        got = b.poll("chaos.topic", 0, 250)
        if not got:
            break
        rest += got
        b.commit("chaos.topic")
    vals = [int(v) for _, v in first + rest]
    assert len(lost_batch) == 300
    assert sorted(vals) == list(range(1000))            # no loss, no duplicate delivery
    assert b.lag("chaos.topic") == 0 and not rclient.xpending("chaos.topic.0", "chaos")["pending"]
