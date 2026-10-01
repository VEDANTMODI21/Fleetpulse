"""Closed-loop API load test (asyncio + httpx). Mixed read workload typical of a
dashboard: at-risk list (keyset pages), vehicle detail, alerts page, fleet summary.
Writes bench/results/api.json with throughput and p50/p95/p99 per endpoint."""
from __future__ import annotations

import asyncio
import json
import os
import random
import time

import httpx
import numpy as np
import psycopg

BASE = os.environ.get("API", "http://localhost:8000")
CONC = int(os.environ.get("CONC", "32"))
DUR = int(os.environ.get("DUR", "45"))


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=10, limits=httpx.Limits(max_connections=CONC)) as c:
        tokens, vins = {}, {}
        with psycopg.connect(os.environ["DATABASE_URL"]) as db:
            for f in range(1, 41):
                vins[f] = [r[0] for r in db.execute("SELECT vin FROM vehicle WHERE fleet_id=%s LIMIT 200", (f,))]
        for f in range(1, 41):
            r = await c.post("/auth/token", json={"email": f"manager{f}@fleet{f}.dev",
                                                   "password": os.environ["DEMO_PASSWORD"]})
            tokens[f] = r.json()["access_token"]
        lat: dict[str, list[float]] = {}
        errors: dict[str, int] = {}
        stop = time.time() + DUR

        async def user(uid):
            rng = random.Random(uid)
            while time.time() < stop:
                f = rng.randint(1, 40)
                h = {"Authorization": f"Bearer {tokens[f]}"}
                x = rng.random()
                if x < 0.35:
                    name, url = "vehicle_detail", f"/api/v1/vehicles/{rng.choice(vins[f])}"
                elif x < 0.65:
                    name, url = "at_risk", "/api/v1/vehicles/at-risk?limit=25"
                elif x < 0.9:
                    name, url = "alerts", "/api/v1/alerts?limit=50"
                else:
                    name, url = "fleet_summary", "/api/v1/fleet/summary"
                t = time.perf_counter()
                r = await c.get(url, headers=h)
                lat.setdefault(name, []).append((time.perf_counter() - t) * 1000)
                if r.status_code >= 400:
                    errors[name] = errors.get(name, 0) + 1
                # follow keyset cursor for 1 in 3 list calls
                if name == "at_risk" and r.status_code == 200 and rng.random() < 0.33 and r.json().get("next_cursor"):
                    t = time.perf_counter()
                    r2 = await c.get(url + "&cursor=" + r.json()["next_cursor"], headers=h)
                    lat.setdefault("at_risk_page2", []).append((time.perf_counter() - t) * 1000)
                    if r2.status_code >= 400:
                        errors["at_risk_page2"] = errors.get("at_risk_page2", 0) + 1

        t0 = time.time()
        await asyncio.gather(*(user(i) for i in range(CONC)))
        wall = time.time() - t0
    allv = [v for xs in lat.values() for v in xs]
    res = {"concurrency": CONC, "duration_s": round(wall, 1), "requests": len(allv), "rps": round(len(allv) / wall, 1),
           "errors": errors,
           "overall_ms": {p: round(float(np.percentile(allv, q)), 1) for p, q in (("p50", 50), ("p95", 95), ("p99", 99))},
           "per_endpoint_ms": {k: {"n": len(v), **{p: round(float(np.percentile(v, q)), 1)
                                                  for p, q in (("p50", 50), ("p95", 95), ("p99", 99))}}
                               for k, v in lat.items()}}
    os.makedirs("bench/results", exist_ok=True)
    json.dump(res, open(os.environ.get("OUT", "bench/results/api.json"), "w"), indent=2)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
