"""Synthetic fleet generator: 100K+ vehicles with realistic attributes.

All data is synthetic. VINs are syntactically valid (with check digit) but random.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

import numpy as np

from fleetpulse.algorithms.vin import make_vin

CITIES = {  # name: (lat, lon)
    "Chennai": (13.0827, 80.2707), "Bangalore": (12.9716, 77.5946), "Mumbai": (19.0760, 72.8777),
    "Delhi": (28.7041, 77.1025), "Hyderabad": (17.3850, 78.4867), "Pune": (18.5204, 73.8567),
    "Surat": (21.1702, 72.8311), "Kolkata": (22.5726, 88.3639),
}
OEMS = ("alpha", "beta", "gamma")
WMI = {"alpha": "1HG", "beta": "2T1", "gamma": "YV1"}
POWERTRAINS = ("ICE", "HYBRID", "EV")
FAULT_MODES = ("cooling", "aux_battery", "misfire", "oil", "hv_battery")
MODES_BY_PT = {
    "ICE": ("cooling", "aux_battery", "misfire", "oil"),
    "HYBRID": ("cooling", "aux_battery", "misfire", "hv_battery"),
    "EV": ("aux_battery", "hv_battery"),
}


@dataclass
class Fleet:
    vin: np.ndarray            # str
    oem: np.ndarray            # str
    powertrain: np.ndarray     # str
    model_year: np.ndarray     # int
    odo_km: np.ndarray         # float
    daily_km: np.ndarray       # float
    city: np.ndarray           # str
    lat: np.ndarray
    lon: np.ndarray
    fleet_id: np.ndarray       # int  (tenant)
    driver_aggr: np.ndarray    # 0..1 aggressiveness

    def __len__(self):
        return len(self.vin)


def generate_fleet(n: int = 100_000, n_fleets: int = 40, seed: int = 7) -> Fleet:
    rng = np.random.default_rng(seed)
    prng = random.Random(seed)
    oem = rng.choice(OEMS, size=n, p=[0.45, 0.35, 0.20])
    pt = rng.choice(POWERTRAINS, size=n, p=[0.6, 0.15, 0.25])
    vins, seen = [], set()
    for o in oem:
        while True:
            v = make_vin(prng, WMI[o])
            if v not in seen:
                seen.add(v)
                vins.append(v)
                break
    my = rng.integers(2016, 2027, size=n)
    age = 2026 - my
    daily = rng.gamma(4.0, 45.0, size=n)                 # ~180 km/day mean, commercial duty
    odo = age * 365 * daily * rng.uniform(0.6, 1.0, n) + rng.uniform(0, 2000, n)
    city_names = np.array(list(CITIES))
    city = rng.choice(city_names, size=n)
    base = np.array([CITIES[c] for c in city])
    lat = base[:, 0] + rng.normal(0, 0.08, n)
    lon = base[:, 1] + rng.normal(0, 0.08, n)
    fleet_id = rng.integers(1, n_fleets + 1, size=n)
    aggr = rng.beta(2, 5, size=n)
    return Fleet(np.array(vins), oem, pt, my, odo, daily, city, lat, lon, fleet_id, aggr)
