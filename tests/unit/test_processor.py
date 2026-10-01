from fleetpulse.domain.models import CanonicalEvent
from fleetpulse.stream.processor import StreamProcessor

VIN = "1M8GDM9AXKP042788"


def ev(seq, ts, **kw):
    base = dict(vin=VIN, ts=ts, seq=seq, oem="alpha", lat=12.9, lon=77.6, speed_kmh=40, odo_km=10, coolant_c=90,
                battery_v=12.5)
    base.update(kw)
    return CanonicalEvent(**base)


def run(events):
    got = []
    sp = StreamProcessor(got.append, clock=lambda: 1000.0)
    for e in events:
        sp.process(e)
    return sp, got


def test_duplicates_and_late_events_do_not_fire():
    sp, got = run([ev(1, 1), ev(1, 1), ev(3, 3), ev(2, 2, evt="BREAKDOWN")])
    assert sp.stats == {"processed": 2, "duplicate": 1, "late": 1, "alerts": 0} and got == []


def test_overheat_needs_sustained_window_and_is_debounced():
    evs = [ev(i, i, coolant_c=130) for i in range(1, 12)]
    sp, got = run(evs)
    kinds = [a.kind for a in got]
    assert kinds.count("OVERHEAT") == 1
    assert sp.latencies and sp.latencies[0] == 1000.0 - 5


def test_severe_dtc_once_per_code_and_breakdown_and_low_voltage_and_oil():
    evs = [ev(1, 1, dtcs=("P0217",)), ev(2, 2, dtcs=("P0217", "P0420"))] + \
          [ev(i, i, battery_v=11.0) for i in range(3, 9)] + \
          [ev(9, 9, evt="BREAKDOWN"), ev(10, 10, evt="HARSH_BRAKE"), ev(11, 11, rpm=2500, oil_pressure_kpa=40)]
    sp, got = run(evs)
    kinds = [a.kind for a in got]
    assert kinds.count("DTC_P0217") == 1 and "DTC_P0420" not in kinds
    assert "BREAKDOWN" in kinds and "LOW_12V" in kinds and "LOW_OIL_PRESSURE" in kinds
    snap = sp.snapshot(VIN)
    assert snap["harsh_15m"] == 1 and snap["active_dtcs"] == ["P0217", "P0420"] and len(snap["geohash"]) == 6
    assert sp.snapshot("NOPE") is None
    assert dict(sp.top_dtcs.items())["P0217"] == 2
