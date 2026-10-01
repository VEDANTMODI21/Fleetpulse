import time

import numpy as np
import pytest

from fleetpulse.ingest.bus import InMemoryBus, partition_for
from fleetpulse.ingest.normalizer import DeclarativeAdapter, Normalizer, ValidationError
from fleetpulse.ingest.pipeline import CANONICAL_TOPIC, DLQ_TOPIC, IngestPipeline, decode, encode
from fleetpulse.simulator.fleet import generate_fleet
from fleetpulse.simulator.stream import StreamConfig, StreamSimulator

VIN = "1M8GDM9AXKP042788"
NOW = 1_790_000_000.0


def norm():
    return Normalizer(lambda: NOW)


def alpha(**kw):
    p = {"vin": VIN, "ts": "2026-09-21T10:00:00Z", "seq": 1, "lat": 12.9, "lon": 77.6, "speed_kmh": 50,
         "odo_km": 1000, "coolant_c": 90, "batt_v": 12.4, "rpm": 2000, "dtc": ["P0301"], "evt": None}
    p.update(kw)
    return p


def test_three_oem_formats_normalise_to_same_units():
    n = norm()
    a = n.normalize("alpha", alpha())
    b = n.normalize("beta", {"vehicle": {"id": VIN}, "t_ms": 1_790_000_000_000, "n": 1, "pos": {"la": 12.9, "lo": 77.6},
                             "mph": 31.0686, "odometer_mi": 621.371, "engine": {"coolant_f": 194, "oil_psi": 30},
                             "elec": {"aux_v": 12.4}, "faults": "0x0301"})
    g = n.normalize("gamma", {"VIN": VIN, "timestamp": "2026-09-21T10:00:00Z", "sequence": 1, "diag": "DTC=P0301",
                              "signals": [{"name": "Vehicle.Speed", "value": 50}, {"name": "Vehicle.Odometer", "value": 1000},
                                          {"name": "Pos.Lat", "value": 12.9}, {"name": "Pos.Lon", "value": 77.6},
                                          {"name": "Engine.CoolantTemp", "value": 90}, {"name": "Unknown", "value": 1}]})
    assert abs(b.speed_kmh - 50) < 0.01 and abs(b.odo_km - 1000) < 0.01 and abs(b.coolant_c - 90) < 0.01
    assert abs(b.oil_pressure_kpa - 206.84) < 0.1
    assert a.dtcs == b.dtcs == g.dtcs == ("P0301",)
    assert g.speed_kmh == 50 and a.received_ts == NOW


@pytest.mark.parametrize("bad", [alpha(vin="1M8GDM9A1KP042788"), alpha(lat=123), alpha(speed_kmh=-4),
                                 alpha(coolant_c="hot"), alpha(ts="2030-01-01T00:00:00Z"), {"vin": VIN},
                                 alpha(batt_v=float("nan"))])
def test_validation_rejects(bad):
    with pytest.raises(ValidationError):
        norm().normalize("alpha", bad)


def test_unknown_oem_rejected():
    with pytest.raises(ValidationError):
        norm().normalize("delta", {})


def test_declarative_adapter_onboards_new_oem_without_code():
    n = norm()
    n.register(DeclarativeAdapter("delta", {
        "vin": "id", "ts": "time", "ts_unit": "ms", "seq": "counter", "dtc": "codes",
        "fields": {"lat": "gps.lat", "lon": "gps.lon", "speed_kmh": "spd", "odo_km": "odo", "coolant_c": "eng.ct"},
        "scale": {"speed_kmh": 1.609344}}))
    ev = n.normalize("delta", {"id": VIN, "time": 1_790_000_000_000, "counter": 7, "codes": "P0217",
                               "gps": {"lat": 1.0, "lon": 2.0}, "spd": 10, "odo": 5, "eng": {"ct": 99}})
    assert "delta" in n.oems and ev.seq == 7 and abs(ev.speed_kmh - 16.09) < 0.01 and ev.dtcs == ("P0217",)
    n.register(DeclarativeAdapter("eps", {"vin": "id", "ts": "t", "seq": "s", "fields": {"lat": "a", "lon": "b",
                                                                                    "speed_kmh": "c", "odo_km": "d"}}))
    assert n.normalize("eps", {"id": VIN, "t": NOW, "s": 1, "a": 0, "b": 0, "c": 0, "d": 0}).ts == NOW
    assert n.normalize("eps", {"id": VIN, "t": "2026-09-21T10:00:00Z", "s": 1, "a": 0, "b": 0, "c": 0, "d": 0}).seq == 1


def test_pipeline_dedup_dlq_and_roundtrip():
    bus = InMemoryBus(4)
    p = IngestPipeline(bus, Normalizer(lambda: NOW), flush_size=10)
    assert p.offer("alpha", alpha()) == "accepted"
    assert p.offer("alpha", alpha()) == "duplicate"
    assert p.offer("alpha", alpha(vin="BAD")) == "invalid"
    p.flush()
    assert bus.lag(DLQ_TOPIC) == 1
    part = partition_for(VIN, 4)
    [(k, v)] = bus.poll(CANONICAL_TOPIC, part)
    ev = decode(v)
    assert k == VIN and ev.dtcs == ("P0301",) and decode(encode(ev)) == ev


def test_pipeline_backpressure():
    p = IngestPipeline(InMemoryBus(1), Normalizer(lambda: NOW), max_buffer=2, flush_size=100)
    assert [p.offer("alpha", alpha(seq=i)) for i in range(3)] == ["accepted", "accepted", "backpressure"]
    assert p.stats["rejected_backpressure"] == 1


def test_partitioning_is_stable_and_spread():
    parts = [partition_for(f"V{i}", 8) for i in range(8000)]
    counts = np.bincount(parts, minlength=8)
    assert partition_for("X", 8) == partition_for("X", 8) and counts.min() > 800


def test_simulator_noise_end_to_end():
    fleet = generate_fleet(2000, seed=3)
    sim = StreamSimulator(fleet, StreamConfig(dup_rate=0.05, ooo_rate=0.05, malformed_rate=0.01))
    bus = InMemoryBus(4)
    pipe = IngestPipeline(bus, Normalizer(time.time))
    for t in range(3):
        for o, raw in sim.tick(time.time() - 10 + t):
            pipe.offer(o, raw)
    pipe.flush()
    s = pipe.stats
    assert s["duplicate"] > 0 and s["invalid"] > 0 and s["accepted"] > 5000
