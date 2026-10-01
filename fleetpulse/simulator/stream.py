"""Real-time raw telemetry generator (1 Hz per vehicle, three OEM formats).

Realism knobs: burst multiplier (shift start / network recovery replays),
duplicate rate, out-of-order rate (events held back and emitted late),
malformed rate, and a set of vehicles in a degrading state that emit rising
coolant temperature / falling 12V voltage / misfire DTCs and eventually a
BREAKDOWN event.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from fleetpulse.simulator.fleet import Fleet


@dataclass
class StreamConfig:
    dup_rate: float = 0.01
    ooo_rate: float = 0.02
    malformed_rate: float = 0.001
    degrading_share: float = 0.002
    seed: int = 5


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class StreamSimulator:
    fleet: Fleet
    cfg: StreamConfig = field(default_factory=StreamConfig)

    def __post_init__(self):
        n = len(self.fleet)
        self.rng = np.random.default_rng(self.cfg.seed)
        self.prng = random.Random(self.cfg.seed)
        # OEM sequence counters are monotonic across simulator restarts (seeded from wall clock)
        self.seq = np.full(n, int(time.time() * 10), np.int64)
        self.odo = self.fleet.odo_km.copy()
        self.lat, self.lon = self.fleet.lat.copy(), self.fleet.lon.copy()
        self.speed = self.rng.uniform(0, 60, n)
        self.health = np.ones(n)                      # 1 = healthy, <0 = broken down
        deg = self.rng.random(n) < self.cfg.degrading_share
        self.decay = np.where(deg, self.rng.uniform(0.002, 0.02, n), 0.0)
        self.held: list[tuple[str, dict]] = []

    def degrade_now(self, idx: int, rate: float = 0.05):
        self.decay[idx] = rate

    def tick(self, ts: float, vehicles: np.ndarray | None = None) -> list[tuple[str, dict]]:
        """One reading for each selected vehicle at time ts. Returns [(oem, raw_payload)]."""
        f, rng = self.fleet, self.rng
        idx = np.arange(len(f)) if vehicles is None else vehicles
        m = len(idx)
        self.speed[idx] = np.clip(self.speed[idx] + rng.normal(0, 4, m), 0, 120)
        self.odo[idx] += self.speed[idx] / 3600
        self.lat[idx] += rng.normal(0, 1e-4, m)
        self.lon[idx] += rng.normal(0, 1e-4, m)
        self.health[idx] -= self.decay[idx]
        sick = 1 - np.clip(self.health[idx], 0, 1)
        coolant = 90 + rng.normal(0, 2, m) + 35 * sick ** 2
        aux_v = 12.4 + rng.normal(0, 0.08, m) - 1.6 * sick ** 2
        rpm = 800 + self.speed[idx] * 30 + rng.normal(0, 50, m)
        harsh = rng.random(m) < 0.0005
        out = []
        for j, i in enumerate(idx):
            self.seq[i] += 1
            evt = None
            dtcs = []
            if self.health[i] < 0:
                evt, self.decay[i], self.health[i] = "BREAKDOWN", 0.0, 1.0
            elif harsh[j]:
                evt = "HARSH_BRAKE"
            if sick[j] > 0.6 and rng.random() < 0.05:
                dtcs.append("P0217")
            out.append((str(f.oem[i]), self._payload(f.oem[i], str(f.vin[i]), ts, int(self.seq[i]), float(self.lat[i]), float(self.lon[i]),
                                               float(self.speed[i]), float(self.odo[i]), float(coolant[j]),
                                               float(aux_v[j]), float(rpm[j]), dtcs, evt)))
        return self._noise(out)

    def _payload(self, oem, vin, ts, seq, lat, lon, spd, odo, cool, auxv, rpm, dtcs, evt):
        if oem == "alpha":
            return {"vin": vin, "ts": _iso(ts), "seq": seq, "lat": round(lat, 5), "lon": round(lon, 5),
                    "speed_kmh": round(spd, 1), "odo_km": round(odo, 1), "coolant_c": round(cool, 1),
                    "batt_v": round(auxv, 2), "rpm": round(rpm), "dtc": dtcs, "evt": evt}
        if oem == "beta":
            return {"vehicle": {"id": vin}, "t_ms": int(ts * 1000), "n": seq, "pos": {"la": lat, "lo": lon},
                    "mph": round(spd / 1.609344, 1), "odometer_mi": round(odo / 1.609344, 1),
                    "engine": {"coolant_f": round(cool * 9 / 5 + 32, 1), "rpm": round(rpm)},
                    "elec": {"aux_v": round(auxv, 2)},
                    "faults": " ".join(f"0x{int(c[1:], 16):04X}" for c in dtcs), "event": evt}
        sig = [{"name": "Vehicle.Speed", "value": round(spd, 1)}, {"name": "Vehicle.Odometer", "value": round(odo, 1)},
               {"name": "Pos.Lat", "value": lat}, {"name": "Pos.Lon", "value": lon},
               {"name": "Engine.CoolantTemp", "value": round(cool, 1)}, {"name": "LV.Voltage", "value": round(auxv, 2)},
               {"name": "Engine.RPM", "value": round(rpm)}]
        if evt:
            sig.append({"name": "Event", "value": evt})
        return {"VIN": vin, "timestamp": _iso(ts), "sequence": seq, "signals": sig,
                "diag": ("DTC=" + ";".join(dtcs)) if dtcs else ""}

    def _noise(self, out):
        c, r = self.cfg, self.prng
        res = []
        # release previously held (late) events first -> they arrive out of order
        released, self.held = self.held, []
        for item in out:
            x = r.random()
            if x < c.ooo_rate:
                self.held.append(item)
                continue
            if x < c.ooo_rate + c.malformed_rate:
                oem, p = item
                p = dict(p)
                p.pop(next(iter(p)))            # drop the VIN/id field
                res.append((oem, p))
                continue
            res.append(item)
            if r.random() < c.dup_rate:
                res.append(item)
        res.extend(released)
        return res
