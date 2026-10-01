import time

from behave import given, then, when

from fleetpulse.api.security import issue_token


def hdr(ctx):
    return {"Authorization": f"Bearer {ctx.token}"}


@given("I am signed in as the fleet manager of the fleet with the riskiest vehicle")
def s_mgr_risky(ctx):
    with ctx.pool.connection() as c:
        ctx.fleet, ctx.risky_vin = c.execute("""SELECT v.fleet_id, v.vin FROM risk_score r JOIN vehicle v ON v.id=r.vehicle_id
                                                ORDER BY r.scored_on DESC, r.p_fail_7d DESC LIMIT 1""").fetchone()
    ctx.token = issue_token(f"manager{ctx.fleet}@bdd", "fleet_manager", ctx.fleet)


@given("I am signed in as the fleet manager of fleet {f:d}")
def s_mgr(ctx, f):
    ctx.fleet, ctx.token = f, issue_token(f"manager{f}@bdd", "fleet_manager", f)


@given("I am signed in as an analyst of fleet {f:d}")
def s_analyst(ctx, f):
    ctx.fleet, ctx.token = f, issue_token(f"analyst{f}@bdd", "analyst", f)


@when("I open the at-risk list")
def w_list(ctx):
    ctx.resp = ctx.client.get("/api/v1/vehicles/at-risk?limit=20", headers=hdr(ctx)).json()


@then("I see vehicles of my fleet only")
def t_only(ctx):
    assert ctx.resp["items"] and all(v["fleet_id"] == ctx.fleet for v in ctx.resp["items"])


@then("they are ordered from highest to lowest 7-day risk")
def t_order(ctx):
    ps = [v["p_fail_7d"] for v in ctx.resp["items"]]
    assert ps == sorted(ps, reverse=True)


@then("each high-risk vehicle explains why it is at risk")
def t_explain(ctx):
    high = [v for v in ctx.resp["items"] if v["p_fail_7d"] >= 0.5]
    assert high and all(v["top_factors"] for v in high)


@when("I ask for a workshop plan with {h:d} technician hours")
def w_plan(ctx, h):
    ctx.plan = ctx.client.get(f"/api/v1/maintenance/plan?budget_hours={h}", headers=hdr(ctx)).json()
    ctx.budget = h


@then("the plan uses at most {h:d} hours")
def t_plan(ctx, h):
    assert 0 < ctx.plan["hours_used"] <= h


@then("every planned vehicle is routed to a depot")
def t_depot(ctx):
    assert all(c["depot_city"] for c in ctx.plan["chosen"])


@when("I ask the copilot to schedule a service for my riskiest vehicle")
def w_copilot(ctx):
    r = ctx.client.post("/api/v1/agent/ask", headers=hdr(ctx),
                        json={"question": f"schedule a service for {ctx.risky_vin}"}).json()
    ctx.answer = r
    with ctx.pool.connection() as c:
        ctx.wo = c.execute("""SELECT w.id, w.status FROM work_order w JOIN vehicle v ON v.id=w.vehicle_id
                              WHERE v.vin=%s ORDER BY w.id DESC LIMIT 1""", (ctx.risky_vin,)).fetchone()


@then('a work order is created with status "{st}"')
def t_wo(ctx, st):
    assert ctx.wo and ctx.wo[1] == st and "approval" in ctx.answer["answer"]


@when("I approve that work order")
def w_approve(ctx):
    ctx.appr = ctx.client.post(f"/api/v1/work-orders/{ctx.wo[0]}/approve", headers=hdr(ctx)).json()


@then('its status becomes "{st}"')
def t_appr(ctx, st):
    assert ctx.appr["status"] == st


@then("the agent action is recorded in the audit log")
def t_audit(ctx):
    with ctx.pool.connection() as c:
        n = c.execute("SELECT count(*) FROM audit_log WHERE action='agent.tool_call' AND detail->>'tool'='propose_work_order'"
                      ).fetchone()[0]
    assert n >= 1


@when("I request the at-risk list of another fleet")
def w_other(ctx):
    ctx.status = ctx.client.get(f"/api/v1/vehicles/at-risk?fleet_id={ctx.fleet % 40 + 1}", headers=hdr(ctx)).status_code


@then("the request is denied with status {code:d}")
def t_denied(ctx, code):
    assert ctx.status == code


@when("I request erasure of a driver in my fleet")
def w_erase(ctx):
    with ctx.pool.connection() as c:
        ctx.driver = c.execute("SELECT id FROM driver WHERE fleet_id=%s AND erased_at IS NULL LIMIT 1",
                               (ctx.fleet,)).fetchone()[0]
    ctx.resp = ctx.client.delete(f"/api/v1/privacy/drivers/{ctx.driver}", headers=hdr(ctx)).json()


@then("the driver's identity is removed")
def t_erased(ctx):
    with ctx.pool.connection() as c:
        assert c.execute("SELECT pseudonym FROM driver WHERE id=%s", (ctx.driver,)).fetchone()[0] == "ERASED"


@then("the audit chain still verifies")
def t_chain(ctx):
    time.sleep(0.3)
    r = ctx.client.get("/api/v1/audit/verify", headers={"Authorization": "Bearer " + issue_token("dpo", "platform_admin", None)})
    assert r.json()["ok"]


@given("a vehicle of fleet {f:d} has a live position")
def g_live(ctx, f):
    with ctx.pool.connection() as c:
        ctx.vin = c.execute("SELECT vin FROM vehicle WHERE fleet_id=%s LIMIT 1", (f,)).fetchone()[0]
    ctx.redis.set(f"veh:{ctx.vin}", b'{"lat":19.076123,"lon":72.877456,"geohash":"te7ud2","speed_kmh":10}')


@when("I open that vehicle")
def w_open(ctx):
    ctx.v = ctx.client.get(f"/api/v1/vehicles/{ctx.vin}", headers=hdr(ctx)).json()


@then("its location is rounded to 2 decimal places")
def t_round(ctx):
    assert ctx.v["live"]["lat"] == 19.08 and ctx.v["live"]["lon"] == 72.88
