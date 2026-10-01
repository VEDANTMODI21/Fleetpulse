"""Multi-OEM normalisation: Adapter pattern + registry (Open/Closed).

Each OEM cloud delivers a different JSON shape and units. Adding a new OEM
means registering one adapter class; no pipeline code changes. Adapters can
also be registered at runtime from a declarative mapping (hot onboarding
without redeploy - see `DeclarativeAdapter`).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from fleetpulse.algorithms.dtc import parse_dtcs
from fleetpulse.algorithms.vin import is_valid_vin
from fleetpulse.domain.models import CanonicalEvent


class ValidationError(ValueError):
    pass


class OemAdapter(Protocol):
    oem: str

    def to_canonical(self, raw: dict[str, Any]) -> CanonicalEvent: ...


def _iso_to_epoch(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _num(v, name, lo=None, hi=None):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError) as e:
        raise ValidationError(f"{name} not numeric") from e
    if f != f:  # NaN
        raise ValidationError(f"{name} is NaN")
    if (lo is not None and f < lo) or (hi is not None and f > hi):
        raise ValidationError(f"{name}={f} out of range")
    return f


class AlphaAdapter:
    """OEM 'Alpha': flat JSON, metric units, ISO timestamps.
    {"vin","ts","lat","lon","speed_kmh","odo_km","coolant_c","batt_v","soc_pct","oil_kpa","rpm","dtc":[..],"evt","seq"}"""
    oem = "alpha"

    def to_canonical(self, r):
        return CanonicalEvent(
            vin=r["vin"], ts=_iso_to_epoch(r["ts"]), seq=int(r["seq"]), oem=self.oem,
            lat=r["lat"], lon=r["lon"], speed_kmh=r["speed_kmh"], odo_km=r["odo_km"],
            coolant_c=r.get("coolant_c"), battery_v=r.get("batt_v"), soc_pct=r.get("soc_pct"),
            oil_pressure_kpa=r.get("oil_kpa"), rpm=r.get("rpm"),
            dtcs=tuple(d.code for d in parse_dtcs(r.get("dtc"))), evt=r.get("evt"))


class BetaAdapter:
    """OEM 'Beta': nested, imperial units, epoch millis, DTCs as hex words.
    {"vehicle":{"id"},"t_ms","pos":{"la","lo"},"mph","odometer_mi","engine":{"coolant_f","oil_psi","rpm"},
     "elec":{"aux_v","soc"},"faults":"0x0301 0x0420","event","n"}"""
    oem = "beta"

    def to_canonical(self, r):
        eng, elec, pos = r.get("engine") or {}, r.get("elec") or {}, r["pos"]
        cf = eng.get("coolant_f")
        psi = eng.get("oil_psi")
        return CanonicalEvent(
            vin=r["vehicle"]["id"], ts=r["t_ms"] / 1000.0, seq=int(r["n"]), oem=self.oem,
            lat=pos["la"], lon=pos["lo"], speed_kmh=r["mph"] * 1.609344, odo_km=r["odometer_mi"] * 1.609344,
            coolant_c=None if cf is None else (cf - 32) * 5 / 9, battery_v=elec.get("aux_v"),
            soc_pct=elec.get("soc"), oil_pressure_kpa=None if psi is None else psi * 6.894757,
            rpm=eng.get("rpm"), dtcs=tuple(d.code for d in parse_dtcs(r.get("faults"))), evt=r.get("event"))


class GammaAdapter:
    """OEM 'Gamma': signal-list format.
    {"VIN","timestamp","sequence","signals":[{"name","value"}...],"diag":"DTC=P0301;P0420"}"""
    oem = "gamma"
    _MAP = {"Vehicle.Speed": "speed_kmh", "Vehicle.Odometer": "odo_km", "Pos.Lat": "lat", "Pos.Lon": "lon",
            "Engine.CoolantTemp": "coolant_c", "LV.Voltage": "battery_v", "HV.SoC": "soc_pct",
            "Engine.OilPressure": "oil_pressure_kpa", "Engine.RPM": "rpm", "Event": "evt"}

    def to_canonical(self, r):
        s = {self._MAP[x["name"]]: x["value"] for x in r["signals"] if x["name"] in self._MAP}
        return CanonicalEvent(
            vin=r["VIN"], ts=_iso_to_epoch(r["timestamp"]), seq=int(r["sequence"]), oem=self.oem,
            lat=s["lat"], lon=s["lon"], speed_kmh=s["speed_kmh"], odo_km=s["odo_km"],
            coolant_c=s.get("coolant_c"), battery_v=s.get("battery_v"), soc_pct=s.get("soc_pct"),
            oil_pressure_kpa=s.get("oil_pressure_kpa"), rpm=s.get("rpm"),
            dtcs=tuple(d.code for d in parse_dtcs(r.get("diag"))), evt=s.get("evt"))


class DeclarativeAdapter:
    """Onboard a new OEM from a JSON mapping (stored in Postgres `oem_mapping`)
    without code deploy. mapping = {"vin": "a.b", "ts": "t", "ts_unit": "ms"|"iso", "fields": {canon: path}, "scale": {canon: k}}"""

    def __init__(self, oem: str, mapping: dict):
        self.oem, self.m = oem, mapping

    @staticmethod
    def _get(r, path):
        for p in path.split("."):
            if r is None:
                return None
            r = r.get(p)
        return r

    def to_canonical(self, r):
        m, g = self.m, self._get
        ts = g(r, m["ts"])
        ts = ts / 1000.0 if m.get("ts_unit") == "ms" else _iso_to_epoch(ts) if isinstance(ts, str) else float(ts)
        vals = {}
        for canon, path in m["fields"].items():
            v = g(r, path)
            if v is not None and canon in m.get("scale", {}):
                v = v * m["scale"][canon]
            vals[canon] = v
        dtc_raw = g(r, m["dtc"]) if "dtc" in m else None
        return CanonicalEvent(vin=g(r, m["vin"]), ts=ts, seq=int(g(r, m["seq"])), oem=self.oem,
                              dtcs=tuple(d.code for d in parse_dtcs(dtc_raw)), **vals)


class Normalizer:
    def __init__(self, clock: Callable[[], float]):
        self._adapters: dict[str, OemAdapter] = {}
        self.clock = clock
        for a in (AlphaAdapter(), BetaAdapter(), GammaAdapter()):
            self.register(a)

    def register(self, adapter: OemAdapter) -> None:
        self._adapters[adapter.oem] = adapter

    @property
    def oems(self) -> list[str]:
        return sorted(self._adapters)

    def normalize(self, oem: str, raw: dict) -> CanonicalEvent:
        adapter = self._adapters.get(oem)
        if adapter is None:
            raise ValidationError(f"unknown OEM '{oem}'")
        try:
            ev = adapter.to_canonical(raw)
        except (KeyError, TypeError, AttributeError) as e:
            raise ValidationError(f"malformed {oem} payload: {e!r}") from e
        self.validate(ev)
        ev.received_ts = self.clock()
        return ev

    def validate(self, ev: CanonicalEvent) -> None:
        if not is_valid_vin(ev.vin):
            raise ValidationError(f"invalid VIN {ev.vin!r}")
        _num(ev.lat, "lat", -90, 90)
        _num(ev.lon, "lon", -180, 180)
        _num(ev.speed_kmh, "speed_kmh", 0, 300)
        _num(ev.odo_km, "odo_km", 0, 3_000_000)
        _num(ev.coolant_c, "coolant_c", -60, 200)
        _num(ev.battery_v, "battery_v", 0, 30)
        _num(ev.soc_pct, "soc_pct", 0, 100)
        now = self.clock()
        if ev.ts > now + 300:
            raise ValidationError("timestamp in the future")
