"""Ingest edge service. OEM clouds push batches (webhook) or the MQTT bridge
forwards device messages here. mTLS is terminated at the gateway/ingress
(client cert CN = OEM id, forwarded as X-Client-Cert-CN); an HMAC API key is
checked as a second factor."""
from __future__ import annotations

import hmac
import os
import time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter, generate_latest
from starlette.responses import Response

from fleetpulse.ingest.bus import bus_from_env
from fleetpulse.ingest.pipeline import IngestPipeline

EVENTS = Counter("ingest_events_total", "Ingested events", ["oem", "result"])
KEYS = {o: os.environ.get(f"INGEST_KEY_{o.upper()}") for o in ("alpha", "beta", "gamma")}
MAX_BATCH = 5000


def create_app(pipeline: IngestPipeline | None = None) -> FastAPI:
    app = FastAPI(title="FleetPulse Ingest", version="1.0.0")
    pipe = pipeline or IngestPipeline(bus_from_env())

    @app.post("/ingest/{oem}")
    async def ingest(oem: str, request: Request, x_api_key: str = Header(default="")):
        expected = KEYS.get(oem)
        if expected is None or not hmac.compare_digest(expected, x_api_key):
            raise HTTPException(401, "unknown OEM or bad key")
        body = await request.body()
        if len(body) > 8 * 1024 * 1024:
            raise HTTPException(413, "batch too large")
        import orjson
        try:
            batch = orjson.loads(body)
        except orjson.JSONDecodeError as e:
            raise HTTPException(400, "invalid JSON") from e
        if not isinstance(batch, list) or len(batch) > MAX_BATCH:
            raise HTTPException(400, f"body must be a JSON array of <= {MAX_BATCH} events")
        res = {"accepted": 0, "duplicate": 0, "invalid": 0, "backpressure": 0}
        for raw in batch:
            r = pipe.offer(oem, raw) if isinstance(raw, dict) else "invalid"
            res[r] += 1
        pipe.flush()
        for k, v in res.items():
            if v:
                EVENTS.labels(oem, k).inc(v)
        if res["backpressure"]:
            # partial accept; client must retry the rejected tail
            return JSONResponse(res, status_code=429, headers={"Retry-After": "1"})
        return res

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "ts": time.time()}

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type="text/plain")

    return app


app = create_app() if os.environ.get("INGEST_AUTOSTART", "1") == "1" else None
