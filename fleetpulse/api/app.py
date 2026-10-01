"""FleetPulse public API (presentation layer). Business rules live in the
repositories / algorithms / agent modules; this file only does HTTP concerns:
auth, validation, rate limiting, DTO shaping, audit and error mapping."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date

import orjson
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

from fleetpulse.agent.copilot import Copilot
from fleetpulse.algorithms.planning import Candidate, nearest_depot, plan_service_slots
from fleetpulse.api.resilience import CircuitBreaker, CircuitOpen, TokenBucket
from fleetpulse.api.security import AuthError, Principal, issue_token, mask_location, verify_token
from fleetpulse.storage import audit
from fleetpulse.storage.repo import Repo

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
WEB_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "web")

LOG = logging.getLogger("fleetpulse.api")
REQS = Counter("api_requests_total", "API requests", ["route", "status"])
LAT = Histogram("api_request_seconds", "API latency", ["route"],
                buckets=(.005, .01, .025, .05, .1, .2, .5, 1, 2))

FAULT_COST = {"cooling": (2500, 450, 2.5), "aux_battery": (1200, 180, 0.5), "misfire": (1800, 300, 1.5),
              "oil": (4000, 600, 3.0), "hv_battery": (6000, 1500, 4.0)}


class TokenReq(BaseModel):
    email: str = Field(max_length=120)
    password: str = Field(max_length=120)


class WorkOrderReq(BaseModel):
    vin: str = Field(min_length=17, max_length=17)
    reason: str = Field(min_length=3, max_length=500)
    scheduled_for: date | None = None


class AskReq(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


class State:
    pool = None
    redis = None
    repo: Repo = None
    limiter: TokenBucket = None
    redis_cb = CircuitBreaker(threshold=3, reset_s=5)
    copilot: Copilot = None
    audit = None


def create_app(pool=None, redis_client=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        if pool is None:
            from psycopg_pool import ConnectionPool
            State.pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=2,
                                        max_size=int(os.environ.get("PG_POOL", "16")), open=True,
                                        reset=reset_conn)
        else:
            State.pool = pool
        if redis_client is None:
            import redis
            State.redis = redis.Redis.from_url(REDIS_URL, socket_timeout=0.2)
        else:
            State.redis = redis_client
        State.repo = Repo(State.pool)
        State.limiter = TokenBucket(float(os.environ.get("RATE_PER_S", "50")), int(os.environ.get("RATE_BURST", "100")),
                                    State.redis if os.environ.get("RATE_BACKEND", "redis") == "redis" else None)
        State.copilot = Copilot(_agent_tools(), lambda action, detail: None)
        State.audit = audit.AuditWriter(State.pool)
        yield
        State.audit.close()

    app = FastAPI(title="FleetPulse API", version="1.0.0", lifespan=lifespan,
                  description="Predictive maintenance for connected fleets. All list endpoints use keyset pagination.")

    # ------------------------------------------------------------------ middleware
    @app.middleware("http")
    async def observe(request: Request, call_next):
        t0 = time.perf_counter()
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        try:
            resp = await call_next(request)
        except Exception:  # noqa: BLE001
            LOG.exception("unhandled error", extra={"request_id": rid, "path": request.url.path})
            resp = problem(500, "internal error", rid)
        route = request.scope.get("route").path if request.scope.get("route") else "unmatched"
        LAT.labels(route).observe(time.perf_counter() - t0)
        REQS.labels(route, resp.status_code).inc()
        resp.headers["x-request-id"] = rid
        resp.headers["strict-transport-security"] = "max-age=63072000; includeSubDomains"
        resp.headers["x-content-type-options"] = "nosniff"
        resp.headers["content-security-policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'"
        return resp

    @app.exception_handler(AuthError)
    async def _auth(_, e: AuthError):
        return problem(e.status, e.msg)

    @app.exception_handler(ValueError)
    async def _val(_, e: ValueError):
        return problem(400, str(e))

    # ------------------------------------------------------------------ auth
    @app.post("/auth/token", tags=["auth"])
    def token(req: TokenReq):
        """Local shared-secret login; production deployments should use an OIDC IdP."""
        expected_password = os.environ.get("DEMO_PASSWORD")
        if not expected_password:
            raise HTTPException(503, "authentication is not configured")
        if not hmac.compare_digest(req.password, expected_password):
            raise AuthError(401, "bad credentials")
        with State.pool.connection() as c:
            row = c.execute("SELECT email, role, fleet_id FROM app_user WHERE email=%s", (req.email,)).fetchone()
        if not row:
            raise AuthError(401, "bad credentials")
        return {"access_token": issue_token(row[0], row[1], row[2]), "token_type": "bearer", "expires_in": 3600,
                "role": row[1], "fleet_id": row[2]}

    def principal(request: Request, authorization: str = Header(default="")) -> Principal:
        if not authorization.lower().startswith("bearer "):
            raise AuthError(401, "missing bearer token")
        p = verify_token(authorization[7:])
        if not State.limiter.allow(p.sub):
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "1"})
        request.state.principal = p
        return p

    def log(p: Principal, action: str, resource: str, detail: dict | None = None):
        if action == "read":      # high-volume data access -> group commit
            State.audit.submit(p.sub, p.fleet_id, action, resource, detail)
            return
        with State.pool.connection() as c:   # mutations & agent actions -> synchronous, in-request
            audit.append(c, p.sub, p.fleet_id, action, resource, detail or {})

    # ------------------------------------------------------------------ vehicles & risk
    @app.get("/api/v1/vehicles/at-risk", tags=["vehicles"])
    def at_risk(p: Principal = Depends(principal), fleet_id: int | None = None,
                limit: int = Query(25, ge=1, le=200), cursor: str | None = Query(None, max_length=200)):
        p.require("vehicles:read")
        f = p.scope_fleet(fleet_id)
        out = cached(f"atrisk:{f}:{limit}:{cursor}", lambda: State.repo.at_risk(f, limit, cursor))
        log(p, "read", "vehicles.at_risk", {"fleet": f, "n": len(out["items"])})
        return json(out)

    @app.get("/api/v1/vehicles/{vin}", tags=["vehicles"])
    def vehicle(vin: str, p: Principal = Depends(principal)):
        p.require("vehicles:read")
        if len(vin) != 17:
            raise ValueError("VIN must be 17 characters")
        v = State.repo.vehicle(vin.upper(), p.scope_fleet(None))
        if not v:
            return problem(404, "vehicle not found")
        v["live"] = live_state(vin.upper(), p)
        log(p, "read", f"vehicle:{vin}")
        return json(v)

    def live_state(vin, p: Principal):
        try:
            raw = State.redis_cb.call(State.redis.get, f"veh:{vin}")
        except (CircuitOpen, Exception):  # noqa: BLE001 - graceful degradation: serve without live data
            return {"available": False}
        if not raw:
            return {"available": False}
        s = orjson.loads(raw)
        s["lat"], s["lon"] = mask_location(s["lat"], s["lon"], p.can("location:precise"))
        if not p.can("location:precise"):
            s["geohash"] = s["geohash"][:5]
        s["available"] = True
        return s

    # ------------------------------------------------------------------ alerts
    @app.get("/api/v1/alerts", tags=["alerts"])
    def alerts(p: Principal = Depends(principal), fleet_id: int | None = None,
               severity: str | None = Query(None, pattern="^(INFO|WARN|CRITICAL)$"),
               status: str | None = Query(None, pattern="^(OPEN|ACK|CLOSED)$"),
               limit: int = Query(50, ge=1, le=200), cursor: str | None = Query(None, max_length=200)):
        p.require("alerts:read")
        out = State.repo.alerts(p.scope_fleet(fleet_id), limit, cursor, severity, status)
        log(p, "read", "alerts", {"n": len(out["items"])})
        return json(out)

    @app.post("/api/v1/alerts/{alert_id}/ack", tags=["alerts"])
    def ack(alert_id: int, p: Principal = Depends(principal)):
        p.require("alerts:ack")
        ok = State.repo.ack_alert(alert_id, p.scope_fleet(None))
        log(p, "ack", f"alert:{alert_id}", {"ok": ok})
        return {"acknowledged": ok}

    @app.get("/api/v1/stream/alerts", tags=["alerts"])
    async def stream_alerts(request: Request, token: str = Query(..., max_length=2000)):
        """Server-sent events of live alerts (tenant-filtered). Token via query because EventSource cannot set headers."""
        p = verify_token(token)
        p.require("alerts:read")
        ps = State.redis.pubsub()
        ps.subscribe("alerts")

        async def gen():
            try:
                while not await request.is_disconnected():
                    m = ps.get_message(timeout=0.0)
                    if m and m["type"] == "message":
                        a = orjson.loads(m["data"])
                        if p.fleet_id is None or a.get("fleet_id") == p.fleet_id:
                            yield f"data: {orjson.dumps(a).decode()}\n\n"
                    else:
                        await asyncio.sleep(0.05)
            finally:
                ps.close()
        return StreamingResponse(gen(), media_type="text/event-stream")

    # ------------------------------------------------------------------ fleet
    @app.get("/api/v1/fleet/summary", tags=["fleet"])
    def summary(p: Principal = Depends(principal), fleet_id: int | None = None):
        p.require("vehicles:read")
        f = p.scope_fleet(fleet_id)
        out = cached(f"summary:{f}", lambda: State.repo.fleet_summary(f))
        try:
            top = State.redis_cb.call(State.redis.get, "stats:topdtc")
            out["live_top_dtcs"] = orjson.loads(top) if top else []
        except Exception:  # noqa: BLE001
            out["live_top_dtcs"] = []
        return json(out)

    # ------------------------------------------------------------------ maintenance planning
    @app.get("/api/v1/maintenance/plan", tags=["maintenance"])
    def plan(p: Principal = Depends(principal), budget_hours: float = Query(40, gt=0, le=400),
             fleet_id: int | None = None):
        p.require("workorders:write")
        f = p.scope_fleet(fleet_id)
        if f is None:
            raise ValueError("fleet_id required for platform admins")
        out = _plan(p, budget_hours, f)
        log(p, "read", "maintenance.plan", {"budget": budget_hours})
        return json(out)

    @app.post("/api/v1/work-orders", status_code=201, tags=["maintenance"])
    def create_wo(req: WorkOrderReq, p: Principal = Depends(principal),
                  idempotency_key: str = Header(..., max_length=100)):
        p.require("workorders:write")
        v = State.repo.vehicle(req.vin.upper(), p.scope_fleet(None))
        if not v:
            return problem(404, "vehicle not found")
        wo = State.repo.create_work_order(v["id"], req.reason, p.sub, f"{p.sub}:{idempotency_key}",
                                          scheduled_for=req.scheduled_for)
        log(p, "create", f"work_order:{wo['id']}", {"vin": req.vin})
        return wo

    @app.post("/api/v1/work-orders/{wo_id}/approve", tags=["maintenance"])
    def approve(wo_id: int, p: Principal = Depends(principal), approve: bool = True):
        p.require("workorders:approve")
        r = State.repo.approve_work_order(wo_id, p.sub, p.scope_fleet(None), approve)
        if not r:
            return problem(404, "work order not found or not pending")
        log(p, "approve" if approve else "reject", f"work_order:{wo_id}")
        return r

    # ------------------------------------------------------------------ agent
    @app.post("/api/v1/agent/ask", tags=["agent"])
    def ask(req: AskReq, p: Principal = Depends(principal)):
        p.require("agent:ask")
        r = State.copilot.ask(p, req.question, audit=lambda action, detail: log(p, action, "copilot", detail))
        return {"answer": r.answer, "tool_calls": r.tool_calls, "refused": r.refused, "grounded": r.grounded,
                "latency_ms": round(r.latency_ms, 1)}

    # ------------------------------------------------------------------ privacy & audit
    @app.delete("/api/v1/privacy/drivers/{driver_id}", tags=["privacy"])
    def erase(driver_id: int, p: Principal = Depends(principal)):
        p.require("privacy:erase")
        rid = State.repo.erase_driver(driver_id, p.scope_fleet(None), p.sub)
        if rid is None:
            return problem(404, "driver not found or already erased")
        log(p, "erase", f"driver:{driver_id}", {"request": rid})
        return {"erasure_request": rid, "status": "completed"}

    @app.get("/api/v1/audit/verify", tags=["privacy"])
    def verify_audit(p: Principal = Depends(principal)):
        p.require("audit:read")
        with State.pool.connection() as c:
            return audit.verify(c)

    # ------------------------------------------------------------------ ops
    @app.get("/api/v1/ops/pipeline", tags=["ops"])
    def ops_pipeline(p: Principal = Depends(principal)):
        """Operator view: consumer lag per partition, worker counters, API latency quantiles."""
        p.require("audit:read")
        parts = int(os.environ.get("PARTITIONS", "8"))
        lag = []
        for i in range(parts):
            try:
                g = [x for x in State.redis.xinfo_groups(f"telemetry.canonical.{i}") if x["name"] == b"stream-processor"]
                lag.append(int(g[0].get("lag") or 0) if g else 0)
            except Exception:  # noqa: BLE001 - stream not created yet
                lag.append(0)
        workers = []
        for k in State.redis.scan_iter("stats:worker:*"):
            workers.append({kk.decode(): float(v) for kk, v in State.redis.hgetall(k).items()})
        q = {}
        for metric in LAT.collect():
            for smp in metric.samples:
                if smp.name.endswith("_count"):
                    q[smp.labels["route"]] = q.get(smp.labels["route"], 0) + smp.value
        return {"ts": time.time(), "lag_per_partition": lag, "workers": workers, "api_requests_by_route": q}

    @app.get("/ops", include_in_schema=False)
    def ops_page():
        return FileResponse(os.path.join(WEB_DIR, "ops.html"))

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        with State.pool.connection() as c:
            c.execute("SELECT 1")
        return {"ok": True, "redis_circuit": State.redis_cb.state}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(WEB_DIR, "index.html"))

    return app


# ---------------------------------------------------------------------- helpers
def cached(key: str, loader, ttl: int = 30):
    """Cache-aside in Redis. Keys are versioned by `risk:version`, which the nightly scoring job
    increments after writing new scores -> explicit invalidation; TTL bounds staleness of alert counts.
    Falls through to the database when Redis is unavailable (circuit breaker)."""
    try:
        ver = State.redis_cb.call(State.redis.get, "risk:version") or b"0"
        k = f"cache:{ver.decode()}:{key}"
        hit = State.redis_cb.call(State.redis.get, k)
        if hit:
            return orjson.loads(hit)
    except Exception:  # noqa: BLE001
        return loader()
    val = orjson.loads(orjson.dumps(loader(), default=str, option=orjson.OPT_NON_STR_KEYS))
    try:
        State.redis_cb.call(State.redis.set, k, orjson.dumps(val), ex=ttl)
    except Exception:  # noqa: BLE001 - cache write is best effort
        LOG.debug("cache write skipped for %s", key)
    return val


def reset_conn(conn):
    """Pool hygiene: repositories switch connections to dict rows; restore tuple rows on return."""
    from psycopg.rows import tuple_row
    conn.row_factory = tuple_row


def problem(status: int, title: str, rid: str | None = None):
    return JSONResponse({"type": "about:blank", "title": title, "status": status, "request_id": rid},
                        status_code=status, media_type="application/problem+json")


def json(obj):
    return Response(orjson.dumps(obj, default=str, option=orjson.OPT_NON_STR_KEYS), media_type="application/json")


def _mode_of(factors):
    s = " ".join(f["signal"] for f in factors)
    for k, m in (("coolant", "cooling"), ("aux_v", "aux_battery"), ("misfire", "misfire"), ("oil", "oil"),
                 ("cell", "hv_battery"), ("soc", "hv_battery")):
        if k in s:
            return m
    return "aux_battery"


def _plan(p: Principal, budget_hours: float, fleet_id: int):
    risky = State.repo.at_risk(fleet_id, 60, None)["items"]
    cands, meta = [], {}
    for v in risky:
        brk, svc, hrs = FAULT_COST[_mode_of(v["top_factors"])]
        c = Candidate(v["vin"], float(v["p_fail_7d"]), brk + 250 + 2.5 * 180, svc, hrs)
        cands.append(c)
        meta[v["vin"]] = v
    chosen, value = plan_service_slots(cands, budget_hours)
    # route each chosen vehicle to the nearest depot with a free bay (Dijkstra over the inter-city road graph)
    depots = State.repo.depots(fleet_id) if fleet_id else []
    graph = city_graph(depots)
    free = {d["city"]: int(d["service_bays"]) for d in depots}
    out = []
    for c in chosen:
        home = meta[c.vehicle_id]["city"]
        hit = nearest_depot(graph, home, free) if home in graph else None
        if hit:
            free[hit[0]] -= 1
        out.append({"vin": c.vehicle_id, "p_fail": c.p_fail, "hours": c.hours, "value": round(c.value, 2),
                    "city": home, "depot_city": hit[0] if hit else None,
                    "route_km": round(hit[1], 1) if hit else None})
    return {"budget_hours": budget_hours, "hours_used": sum(c.hours for c in chosen), "expected_saving": round(value, 2),
            "chosen": out}


def city_graph(depots, max_km: float = 800.0):
    """Road-graph approximation: edges between depot cities closer than max_km, weight = 1.25 x great-circle km."""
    from math import asin, cos, radians, sin, sqrt

    def hav(a, b):
        dlat, dlon = radians(b["lat"] - a["lat"]), radians(b["lon"] - a["lon"])
        h = sin(dlat / 2) ** 2 + cos(radians(a["lat"])) * cos(radians(b["lat"])) * sin(dlon / 2) ** 2
        return 2 * 6371 * asin(sqrt(h))
    g = {d["city"]: [] for d in depots}
    for a in depots:
        for b in depots:
            if a is not b and hav(a, b) <= max_km:
                g[a["city"]].append((b["city"], 1.25 * hav(a, b)))
    return g


def _agent_tools():
    def at_risk(p, limit=5):
        return State.repo.at_risk(p.scope_fleet(None), limit, None)

    def vehicle(p, vin):
        return State.repo.vehicle(vin, p.scope_fleet(None))

    def propose(p, vin, reason):
        v = State.repo.vehicle(vin, p.scope_fleet(None))
        if not v:
            return {"error": "NotFound"}
        key = "agent:" + hashlib.sha256(f"{vin}:{reason}".encode()).hexdigest()[:24]
        return State.repo.create_work_order(v["id"], "[copilot] " + reason, f"agent:copilot<{p.sub}>", key)

    def plan_week(p, budget_hours=40.0):
        return _plan(p, budget_hours, p.scope_fleet(None))

    return {"at_risk": at_risk, "vehicle": vehicle, "propose_work_order": propose, "plan_week": plan_week}


app = create_app()
