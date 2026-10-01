"""Stream processor: canonical events -> per-vehicle state, real-time alerts,
live features, heavy-hitter DTCs.

Per partition this is single-threaded (one consumer owns a VIN), so state is
lock-free. Scale-out = more partitions + more replicas (consumer group).

Ordering / idempotency
* Per-VIN high-water mark on `seq`: duplicates (seq == last) are dropped;
  late events (seq < last) are accepted into the lake but do not move state or
  fire alerts (out-of-order tolerance without re-ordering buffers).
Complexity: O(1) amortised per event; memory O(V * window) for V vehicles.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from fleetpulse.algorithms.dtc import SEVERE_PREFIXES
from fleetpulse.algorithms.streaming import SlidingWindow, TopK, geohash_encode
from fleetpulse.domain.models import Alert, CanonicalEvent, Severity

COOLANT_CRIT_C = 115.0
AUX_V_WARN = 11.8
OIL_KPA_CRIT = 70.0
ALERT_COOLDOWN_S = 600


@dataclass(slots=True)
class VehicleState:
    last_seq: int = -1
    last_ts: float = 0.0
    lat: float = 0.0
    lon: float = 0.0
    speed: float = 0.0
    odo: float = 0.0
    coolant: SlidingWindow = field(default_factory=lambda: SlidingWindow(900))
    aux_v: SlidingWindow = field(default_factory=lambda: SlidingWindow(900))
    last_alert: dict = field(default_factory=dict)
    dtcs: set = field(default_factory=set)
    harsh_15m: int = 0


class StreamProcessor:
    def __init__(self, on_alert: Callable[[Alert], None], clock: Callable[[], float] = time.time):
        self.state: dict[str, VehicleState] = {}
        self.on_alert = on_alert
        self.clock = clock
        self.top_dtcs = TopK(10)
        self.latencies: list[float] = []
        self.stats = {"processed": 0, "duplicate": 0, "late": 0, "alerts": 0}

    def process(self, ev: CanonicalEvent) -> list[Alert]:
        st = self.state.get(ev.vin)
        if st is None:
            st = self.state[ev.vin] = VehicleState()
        if ev.seq == st.last_seq:
            self.stats["duplicate"] += 1
            return []
        if ev.seq < st.last_seq:
            self.stats["late"] += 1
            return []
        st.last_seq, st.last_ts = ev.seq, ev.ts
        st.lat, st.lon, st.speed, st.odo = ev.lat, ev.lon, ev.speed_kmh, ev.odo_km
        if ev.coolant_c is not None:
            st.coolant.add(ev.ts, ev.coolant_c)
        if ev.battery_v is not None:
            st.aux_v.add(ev.ts, ev.battery_v)
        alerts = []
        for code in ev.dtcs:
            self.top_dtcs.add(code)
            if code not in st.dtcs:
                st.dtcs.add(code)
                if code.startswith(SEVERE_PREFIXES):
                    alerts.append(self._alert(st, ev, f"DTC_{code}", Severity.CRITICAL, f"Severe DTC {code} reported"))
        if ev.evt == "BREAKDOWN":
            alerts.append(self._alert(st, ev, "BREAKDOWN", Severity.CRITICAL, "Vehicle reported breakdown", cooldown=0))
        elif ev.evt == "HARSH_BRAKE":
            st.harsh_15m += 1
        if ev.coolant_c is not None and st.coolant.mean > COOLANT_CRIT_C and len(st.coolant.q) >= 5:
            alerts.append(self._alert(st, ev, "OVERHEAT", Severity.CRITICAL,
                                      f"Coolant 15-min mean {st.coolant.mean:.1f}C > {COOLANT_CRIT_C}C", st.coolant.mean))
        if ev.battery_v is not None and st.aux_v.mean < AUX_V_WARN and len(st.aux_v.q) >= 5:
            alerts.append(self._alert(st, ev, "LOW_12V", Severity.WARN,
                                      f"12V battery mean {st.aux_v.mean:.2f}V", st.aux_v.mean))
        if ev.oil_pressure_kpa is not None and ev.rpm and ev.rpm > 1500 and ev.oil_pressure_kpa < OIL_KPA_CRIT:
            alerts.append(self._alert(st, ev, "LOW_OIL_PRESSURE", Severity.CRITICAL,
                                      f"Oil pressure {ev.oil_pressure_kpa:.0f} kPa at {ev.rpm:.0f} rpm", ev.oil_pressure_kpa))
        alerts = [a for a in alerts if a is not None]
        for a in alerts:
            self.on_alert(a)
            self.latencies.append(self.clock() - ev.ts)
        self.stats["processed"] += 1
        self.stats["alerts"] += len(alerts)
        return alerts

    def _alert(self, st, ev, kind, sev, detail, value=None, cooldown=ALERT_COOLDOWN_S):
        last = st.last_alert.get(kind, -1e18)
        if ev.ts - last < cooldown:
            return None   # de-bounce: one alert per kind per vehicle per 10 min
        st.last_alert[kind] = ev.ts
        return Alert(ev.vin, ev.ts, kind, sev, detail, value)

    def snapshot(self, vin: str) -> dict | None:
        st = self.state.get(vin)
        if st is None:
            return None
        return {"vin": vin, "ts": st.last_ts, "lat": st.lat, "lon": st.lon, "geohash": geohash_encode(st.lat, st.lon, 6),
                "speed_kmh": st.speed, "odo_km": st.odo, "coolant_mean_15m": round(st.coolant.mean, 2),
                "coolant_slope_per_h": round(st.coolant.slope, 2), "aux_v_mean_15m": round(st.aux_v.mean, 3),
                "active_dtcs": sorted(st.dtcs), "harsh_15m": st.harsh_15m}
