"""Domain layer: canonical event + value objects. No framework imports."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Powertrain(str, Enum):
    ICE = "ICE"
    HYBRID = "HYBRID"
    EV = "EV"


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


@dataclass(slots=True)
class CanonicalEvent:
    """One normalised telemetry reading, independent of OEM format."""
    vin: str
    ts: float                 # epoch seconds (UTC)
    seq: int
    oem: str
    lat: float
    lon: float
    speed_kmh: float
    odo_km: float
    coolant_c: float | None = None
    battery_v: float | None = None     # 12V auxiliary battery
    soc_pct: float | None = None       # EV/hybrid traction battery
    oil_pressure_kpa: float | None = None
    rpm: float | None = None
    dtcs: tuple[str, ...] = ()
    evt: str | None = None             # e.g. HARSH_BRAKE, IGN_ON, BREAKDOWN
    received_ts: float = 0.0


@dataclass(slots=True)
class Alert:
    vin: str
    ts: float
    kind: str
    severity: Severity
    detail: str
    value: float | None = None
    fleet_id: str | None = None


@dataclass
class RiskScore:
    vin: str
    p_fail_7d: float
    top_factors: list[tuple[str, float]] = field(default_factory=list)
    model_version: str = "gbm-v1"
