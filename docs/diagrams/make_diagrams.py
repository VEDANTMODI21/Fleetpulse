"""Generates the architecture / ER / deployment / sequence diagrams as SVG + PNG (docs/img/diag_*.png)."""
from __future__ import annotations

import html
import os

from playwright.sync_api import sync_playwright

DASH = ' stroke-dasharray="5 4"'
OUT = os.path.join(os.path.dirname(__file__), "..", "img")
C = {"person": "#1f6feb", "ext": "#6e7781", "svc": "#0969da", "store": "#8250df", "bus": "#bf8700", "sys": "#1a7f37",
     "zone": "#f6f8fa"}


def esc(s):
    return html.escape(s)


def box(x, y, w, h, title, sub="", kind="svc", shape="rect"):
    col = C[kind]
    t = [f'<g>']
    if shape == "cyl":
        t.append(f'<path d="M{x},{y + 8} v{h - 16} a{w / 2},8 0 0 0 {w},0 v{-(h - 16)}" fill="#fff" stroke="{col}" stroke-width="2"/>'
                 f'<ellipse cx="{x + w / 2}" cy="{y + 8}" rx="{w / 2}" ry="8" fill="#fff" stroke="{col}" stroke-width="2"/>')
    elif shape == "zone":
        t.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{C["zone"]}" stroke="#d0d7de" stroke-dasharray="6 4"/>')
        t.append(f'<text x="{x + 10}" y="{y + 18}" font-size="12" font-weight="700" fill="#57606a">{esc(title)}</text></g>')
        return "".join(t)
    else:
        t.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="#fff" stroke="{col}" stroke-width="2"/>'
                 f'<rect x="{x}" y="{y}" width="6" height="{h}" rx="3" fill="{col}"/>')
    lines = sub.split("\n") if sub else []
    ty = y + h / 2 - (len(lines) * 7) + 4 + (7 if shape == "cyl" else 0)
    t.append(f'<text x="{x + w / 2 + 3}" y="{ty}" text-anchor="middle" font-size="13" font-weight="700" fill="#1f2328">{esc(title)}</text>')
    for i, ln in enumerate(lines):
        t.append(f'<text x="{x + w / 2 + 3}" y="{ty + 15 + i * 13}" text-anchor="middle" font-size="10.5" fill="#57606a">{esc(ln)}</text>')
    t.append("</g>")
    return "".join(t)


def arrow(pts, label="", dash=False, lx=None, ly=None, col="#57606a"):
    d = "M" + " L".join(f"{x},{y}" for x, y in pts)
    s = f'<path d="{d}" fill="none" stroke="{col}" stroke-width="1.6" marker-end="url(#a)"{DASH if dash else ""}/>'
    if label:
        (x1, y1), (x2, y2) = pts[len(pts) // 2 - 1], pts[len(pts) // 2]
        lx = lx if lx is not None else (x1 + x2) / 2
        ly = ly if ly is not None else (y1 + y2) / 2 - 5
        for i, ln in enumerate(label.split("\n")):
            s += (f'<text x="{lx}" y="{ly + i * 12}" text-anchor="middle" font-size="10.5" fill="#1f2328" '
                  f'paint-order="stroke" stroke="#fff" stroke-width="4">{esc(ln)}</text>')
    return s


def svg(w, h, body, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'font-family="Segoe UI, Helvetica, Arial, sans-serif"><defs><marker id="a" viewBox="0 0 10 10" refX="9" '
            f'refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#57606a"/>'
            f'</marker></defs><rect width="100%" height="100%" fill="#fff"/>'
            f'<text x="16" y="26" font-size="16" font-weight="700" fill="#1f2328">{esc(title)}</text>{body}</svg>')


def context():
    b = [box(40, 70, 170, 60, "Fleet manager", "web dashboard, copilot", "person"),
         box(40, 170, 170, 60, "Technician", "work orders", "person"),
         box(40, 270, 170, 60, "Platform admin / DPO", "ops, audit, erasure", "person"),
         box(360, 150, 260, 110, "FleetPulse", "predictive maintenance platform\n(ingest, stream, batch, API, agent)", "sys"),
         box(770, 50, 200, 60, "OEM clouds (3+)", "Alpha / Beta / Gamma", "ext"),
         box(770, 135, 200, 60, "Vehicle gateways", "MQTT (device-free / dongle)", "ext"),
         box(770, 220, 200, 60, "Identity provider", "OIDC (Keycloak / Entra)", "ext"),
         box(770, 305, 200, 60, "LLM API (optional)", "copilot planner", "ext"),
         box(360, 330, 260, 60, "Workshop / ERP", "approved work orders (future)", "ext"),
         arrow([(210, 100), (360, 180)], "HTTPS"), arrow([(210, 200), (360, 205)], "HTTPS"),
         arrow([(210, 300), (360, 235)], "HTTPS"),
         arrow([(770, 80), (620, 175)], "HTTPS webhook\n(mTLS)"), arrow([(770, 165), (620, 195)], "MQTT/TLS"),
         arrow([(620, 225), (770, 250)], "OIDC / JWKS"), arrow([(620, 250), (770, 330)], "HTTPS", dash=True),
         arrow([(490, 260), (490, 330)], "events", dash=True)]
    return svg(1000, 410, "".join(b), "C4 Level 1 - System context")


def container():
    b = [box(20, 50, 1160, 475, "FleetPulse (Kubernetes namespace fleetpulse)", shape="zone"),
         box(-200, 0, 0, 0, ""),
         box(40, 90, 150, 60, "OEM clouds", "3 payload formats", "ext"),
         box(40, 380, 150, 60, "Web UI", "SPA (static, CSP)", "person"),
         box(240, 90, 170, 64, "Ingest service", "FastAPI · validate · dedup\nback-pressure (429)"),
         box(470, 90, 170, 64, "Kafka", "telemetry.canonical (48 p)\ntelemetry.dlq · alerts", "bus", "cyl"),
         box(700, 90, 170, 64, "Stream workers", "per-VIN state · rules\nwindows · Top-K"),
         box(930, 60, 200, 56, "Redis", "live state · cache · rate\nlimits · pub/sub", "store", "cyl"),
         box(930, 150, 200, 56, "Object store lake", "Parquet (hot/warm/cold)", "store", "cyl"),
         box(930, 250, 200, 64, "PostgreSQL 16", "3NF core · alerts · risk\naudit (hash chain)", "store", "cyl"),
         box(930, 350, 200, 56, "pgvector", "repair KB embeddings", "store", "cyl"),
         box(700, 240, 170, 70, "Batch jobs", "DuckDB features · GBM\nscoring (nightly CronJob)"),
         box(470, 380, 170, 64, "Public API", "FastAPI · JWT/RBAC · keyset\nSSE · rate limit"),
         box(700, 380, 170, 64, "Copilot agent", "tool allow-list · guardrails\nhuman approval"),
         box(240, 455, 170, 50, "Prometheus/Grafana", "metrics · SLO alerts", "ext"),
         arrow([(190, 120), (240, 120)], "HTTPS"),
         arrow([(410, 120), (470, 120)], "Kafka\n(idempotent)"),
         arrow([(640, 120), (700, 120)], "consume\n(group)"),
         arrow([(870, 105), (930, 90)], "SET/PUB"),
         arrow([(870, 130), (930, 175)], "Parquet"),
         arrow([(870, 145), (930, 270)], "INSERT\nON CONFLICT"),
         arrow([(1030, 206), (870, 262)], "read lake", lx=965, ly=222),
         arrow([(870, 290), (930, 290)], "UPSERT"),
         arrow([(190, 410), (470, 410)], "HTTPS / SSE", lx=330, ly=400),
         arrow([(640, 400), (930, 300)], "SQL (pool)", lx=760, ly=348),
         arrow([(640, 390), (930, 100)], "GET / pub-sub", lx=700, ly=330),
         arrow([(640, 425), (700, 425)], "tools"),
         arrow([(870, 410), (930, 385)], "kNN"),
         arrow([(500, 444), (410, 475)], "/metrics", lx=470, ly=478)]
    return svg(1200, 540, "".join(b), "C4 Level 2 - Containers (protocols on arrows)")


def dataflow():
    hops = [("Vehicle / OEM cloud", "event generated"), ("Ingest", "parse, validate,\nVIN check, dedup"),
            ("Kafka", "partition by VIN"), ("Stream worker", "state + rules"), ("Redis pub/sub", "fan-out"),
            ("API SSE", "tenant filter"), ("Dashboard", "alert rendered")]
    lat = ["batch window\n(sim: 1 s tick)", "~28 us/event\n(35K ev/s/core)", "< 5 ms (est.)", "~15 us/event", "< 1 ms (est.)",
           "50 ms poll"]
    b, x = [], 20
    for i, (t, s) in enumerate(hops):
        b.append(box(x, 70, 140, 64, t, s, "bus" if t == "Kafka" else ("person" if i in (0, 6) else "svc")))
        if i < len(hops) - 1:
            b.append(arrow([(x + 140, 102), (x + 170, 102)]))
            b.append(f'<text x="{x + 155}" y="160" text-anchor="middle" font-size="10.5" fill="#57606a">'
                     + "".join(f'<tspan x="{x + 155}" dy="{0 if j == 0 else 12}">{esc(ln)}</tspan>'
                               for j, ln in enumerate(lat[i].split("\n"))) + "</text>")
        x += 170
    b.append('<text x="20" y="215" font-size="12" fill="#1f2328">Measured end to end (event timestamp -> alert received by a '
             'subscriber) on 2 vCPU: p50 0.50 s, p95 0.99 s, p99 1.36 s at 8K ev/s with 24K ev/s bursts (target: critical alert &lt; 5 s).</text>')
    return svg(1200, 235, "".join(b), "Data flow of one telemetry event (latency per hop)")


def er():
    ents = {
        "fleet": (40, 60, ["id PK", "name UQ", "segment"]),
        "subscription": (40, 210, ["id PK", "fleet_id FK", "plan", "vehicle_limit", "valid_from/to"]),
        "app_user": (40, 380, ["id PK", "email UQ", "fleet_id FK?", "role"]),
        "depot": (270, 60, ["id PK", "fleet_id FK", "city, lat, lon", "service_bays"]),
        "vehicle": (500, 60, ["id PK", "vin UQ (CHECK)", "fleet_id FK", "model_id FK", "home_depot_id FK"]),
        "vehicle_model": (740, 60, ["id PK", "oem_id FK", "name, powertrain", "model_year"]),
        "oem": (980, 60, ["id PK", "code UQ", "name"]),
        "oem_mapping": (980, 210, ["(oem_id, version) PK", "mapping JSONB", "active"]),
        "driver": (270, 250, ["id PK", "fleet_id FK", "pseudonym", "licence_hash", "erased_at"]),
        "vehicle_assignment": (270, 420, ["(vehicle_id, from_ts) PK", "driver_id FK", "to_ts"]),
        "trip": (500, 420, ["id PK", "vehicle_id FK", "driver_id FK?", "start/end, km"]),
        "alert": (500, 250, ["id PK", "vehicle_id FK", "fleet_id FK (denorm)", "ts, kind, severity", "status, dedup_key UQ"]),
        "risk_score": (740, 250, ["(vehicle_id, scored_on) PK", "p_fail_7d", "model_version", "top_factors JSONB"]),
        "work_order": (740, 420, ["id PK", "vehicle_id FK", "depot_id FK", "status, proposed_by", "approved_by, idem_key UQ"]),
        "vehicle_daily": (980, 350, ["(vehicle_id, day) PK", "partition by month", "km, coolant_max ...", "dtcs, breakdown"]),
        "dtc_catalog": (980, 500, ["code PK", "system", "description", "severity"]),
        "audit_log": (40, 520, ["id PK", "actor, action", "resource, detail", "prev_hash, hash"]),
    }
    b, pos = [], {}
    for n, (x, y, cols) in ents.items():
        h = 26 + 15 * len(cols)
        pos[n] = (x, y, 200, h)
        b.append(f'<rect x="{x}" y="{y}" width="200" height="{h}" rx="6" fill="#fff" stroke="#8250df" stroke-width="1.6"/>'
                 f'<rect x="{x}" y="{y}" width="200" height="22" rx="6" fill="#8250df"/>'
                 f'<text x="{x + 8}" y="{y + 15}" font-size="12" font-weight="700" fill="#fff">{n}</text>')
        for i, cname in enumerate(cols):
            b.append(f'<text x="{x + 8}" y="{y + 37 + i * 15}" font-size="10.5" fill="#1f2328">{esc(cname)}</text>')

    def link(a, c):
        ax, ay, aw, ah = pos[a]
        cx, cy, cw, ch = pos[c]
        p1 = (ax + aw / 2, ay + ah / 2)
        p2 = (cx + cw / 2, cy + ch / 2)
        return f'<line x1="{p1[0]}" y1="{p1[1]}" x2="{p2[0]}" y2="{p2[1]}" stroke="#afb8c1" stroke-width="1.3"/>'
    rels = [("fleet", "subscription"), ("fleet", "app_user"), ("fleet", "depot"), ("depot", "vehicle"),
            ("vehicle", "vehicle_model"), ("vehicle_model", "oem"), ("oem", "oem_mapping"), ("fleet", "driver"),
            ("driver", "vehicle_assignment"), ("vehicle", "alert"), ("vehicle", "risk_score"), ("vehicle", "work_order"),
            ("vehicle", "trip"), ("vehicle", "vehicle_daily"), ("vehicle_assignment", "vehicle")]
    lines = "".join(link(a, c) for a, c in rels)
    note = ('<text x="270" y="630" font-size="11" fill="#57606a">Lines are 1:N foreign-key relations. '
            'Only deliberate denormalisation: alert.fleet_id (tenant filter without join, alerts immutable).</text>')
    return svg(1200, 650, lines + "".join(b) + note, "Relational core - ER diagram (3NF)")


def deployment():
    b = [box(20, 45, 1160, 470, "AWS ap-south-1 (Terraform) - same Helm chart on GKE / AKS / kind", shape="zone"),
         box(40, 80, 700, 300, "EKS cluster - namespace fleetpulse (3 AZ, NetworkPolicy default-deny)", shape="zone"),
         box(60, 115, 200, 70, "Ingress-NGINX", "TLS 1.3 · mTLS on ingest host\nrate limit", "ext"),
         box(290, 110, 200, 60, "api Deployment", "3-30 pods · HPA cpu 60%\nPDB minAvailable 2"),
         box(290, 190, 200, 60, "ingest Deployment", "4-40 pods · HPA cpu 65%"),
         box(520, 110, 200, 60, "worker StatefulSet", "6-48 pods · KEDA on\nconsumer lag"),
         box(520, 190, 200, 60, "nightly-scoring CronJob", "features + GBM scoring"),
         box(60, 290, 200, 70, "External Secrets", "Secrets Manager -> k8s\nSecret (JWT, DB, keys)", "ext"),
         box(290, 290, 200, 70, "Prometheus + Grafana", "SLO alerts", "ext"),
         box(520, 290, 200, 70, "topologySpread zones", "non-root, read-only FS\nseccomp RuntimeDefault", "ext"),
         box(780, 80, 380, 300, "Managed data services (private subnets, KMS AES-256)", shape="zone"),
         box(800, 115, 160, 60, "MSK Kafka", "3 brokers · RF 3\nmin.insync 2 · TLS", "bus", "cyl"),
         box(980, 115, 160, 60, "RDS PostgreSQL", "Multi-AZ + read replica\nPITR 14 d", "store", "cyl"),
         box(800, 205, 160, 60, "ElastiCache Redis", "primary + replica\nTLS, auto-failover", "store", "cyl"),
         box(980, 205, 160, 60, "S3 lake", "Std 30d -> IA 180d\n-> Glacier IR 2y", "store", "cyl"),
         box(800, 295, 340, 60, "KMS · Secrets Manager · IAM (IRSA)", "", "ext"),
         box(40, 410, 1120, 80, "CI/CD (GitHub Actions)", "lint · SAST (Bandit, Semgrep) · pip-audit · pytest (Postgres+Redis) · behave · Trivy image scan · SBOM ·\nZAP DAST · helm lint · terraform validate · model quality gate -> push image -> helm upgrade (rolling, maxUnavailable 0)", "sys"),
         arrow([(260, 150), (290, 140)]), arrow([(260, 160), (290, 215)]),
         arrow([(490, 220), (520, 140)], "produce"), arrow([(720, 140), (800, 145)])]
    return svg(1200, 530, "".join(b), "Deployment view")


def layers():
    rows = [("Presentation / API", "fleetpulse/api/app.py, ingest/service.py", "HTTP, SSE, auth checks, DTOs, RFC7807 errors", "#1f6feb"),
            ("Application", "ingest/pipeline.py, stream/worker.py, agent/copilot.py, ml/score.py", "use cases, orchestration, transactions", "#1a7f37"),
            ("Domain", "domain/models.py, algorithms/*, stream/processor.py", "entities, rules, algorithms - no framework imports", "#8250df"),
            ("Infrastructure (adapters)", "ingest/bus.py (Kafka|Redis|Memory), storage/repo.py, storage/audit.py, agent/kb.py", "DB, broker, cache, vector store", "#bf8700")]
    b = []
    for i, (n, f, r, col) in enumerate(rows):
        y = 50 + i * 78
        b.append(f'<rect x="40" y="{y}" width="820" height="62" rx="8" fill="#fff" stroke="{col}" stroke-width="2"/>'
                 f'<rect x="40" y="{y}" width="8" height="62" rx="3" fill="{col}"/>'
                 f'<text x="64" y="{y + 24}" font-size="14" font-weight="700" fill="#1f2328">{esc(n)}</text>'
                 f'<text x="64" y="{y + 44}" font-size="11" fill="#57606a">{esc(f)}</text>'
                 f'<text x="850" y="{y + 24}" font-size="11" fill="#1f2328" text-anchor="end">{esc(r)}</text>')
        if i < 3:
            b.append(arrow([(900, y + 31), (900, y + 109)], ""))
    b.append('<text x="912" y="170" font-size="11" fill="#1f2328">depends on</text>'
             '<text x="912" y="184" font-size="11" fill="#57606a">(inward only;</text>'
             '<text x="912" y="198" font-size="11" fill="#57606a">adapters implement</text>'
             '<text x="912" y="212" font-size="11" fill="#57606a">ports, e.g. EventBus)</text>')
    return svg(1040, 370, "".join(b), "Hexagonal layering of the stream / ingest services")


def sequence(title, actors, msgs, h):
    w = 1200
    gap = (w - 120) / (len(actors) - 1)
    gap = (w - 220) / (len(actors) - 1)
    xs = [110 + i * gap for i in range(len(actors))]
    b = []
    for x, a in zip(xs, actors):
        b.append(f'<rect x="{x - 75}" y="45" width="150" height="34" rx="6" fill="#fff" stroke="#0969da" stroke-width="1.6"/>'
                 f'<text x="{x}" y="67" text-anchor="middle" font-size="12" font-weight="700">{esc(a)}</text>'
                 f'<line x1="{x}" y1="79" x2="{x}" y2="{h - 10}" stroke="#d0d7de" stroke-dasharray="4 4"/>')
    y = 105
    for m in msgs:
        if m[0] == "note":
            cx = (xs[m[1]] + xs[m[2]]) / 2
            nw = min(w - 20, max(xs[m[2]] - xs[m[1]] + 120, len(m[3]) * 6.6 + 30))
            b.append(f'<rect x="{max(10, cx - nw / 2)}" y="{y - 13}" width="{nw}" height="22" rx="4" fill="#fff8c5" stroke="#d4a72c"/>'
                     f'<text x="{cx}" y="{y + 2}" text-anchor="middle" font-size="11">{esc(m[3])}</text>')
        else:
            a, c, label, *opt = m
            col = "#d1242f" if opt and opt[0] == "err" else "#57606a"
            if a == c:
                b.append(f'<path d="M{xs[a]},{y - 6} h40 v14 h-38" fill="none" stroke="{col}" marker-end="url(#a)"/>'
                         f'<text x="{xs[a] + 46}" y="{y + 4}" font-size="11" fill="{col}">{esc(label)}</text>')
            else:
                b.append(f'<line x1="{xs[a]}" y1="{y}" x2="{xs[c]}" y2="{y}" stroke="{col}" stroke-width="1.5" '
                         f'marker-end="url(#a)"{DASH if opt and opt[0] == "ret" else ""}/>'
                         f'<text x="{(xs[a] + xs[c]) / 2}" y="{y - 5}" text-anchor="middle" font-size="11" fill="{col}">{esc(label)}</text>')
        y += 30
    return svg(w, h, "".join(b), title)


def seq_alert():
    A = ["OEM cloud", "Ingest", "Kafka", "Stream worker", "PostgreSQL", "Redis", "Dashboard (SSE)"]
    m = [(0, 1, "POST /ingest/beta [batch]  (mTLS + key)"), (1, 1, "adapter -> canonical, VIN check digit, ranges"),
         (1, 1, "Bloom(vin:seq) -> new"), (1, 2, "produce key=VIN (acks=all, idempotent)"), (1, 0, "200 {accepted, duplicate, invalid}", "ret"),
         (2, 3, "poll partition(VIN)"), (3, 3, "seq > high-water mark; windows; rule OVERHEAT fires"),
         (3, 4, "INSERT alert ... ON CONFLICT(dedup_key) DO NOTHING"), (3, 5, "PUBLISH alerts / SET veh:{vin}"),
         (5, 6, "SSE event (tenant-filtered)"), ("note", 3, 6, "measured event -> subscriber: p95 0.99 s"),
         (3, 2, "commit offsets after sinks are durable (at-least-once)")]
    return sequence("Sequence 1 - telemetry to real-time alert (happy path)", A, m, 470)


def seq_fail():
    A = ["OEM cloud", "Ingest", "Kafka / Streams", "Worker-0", "Worker-0 (restarted)", "PostgreSQL"]
    m = [(0, 1, "POST batch (retry of a timed-out request)"), (1, 1, "Bloom says (vin,seq) seen -> duplicate, dropped"),
         (0, 1, "payload in NEW OEM format"), (1, 2, "-> telemetry.dlq {error, raw}", "err"), (1, 0, "200 {invalid: n} + IngestInvalidSpike alert", "ret"),
         ("note", 1, 2, "onboard: INSERT oem_mapping v2 -> DeclarativeAdapter (no deploy); replay DLQ"),
         (2, 3, "poll 2,000 events"), (3, 3, "SIGKILL before commit", "err"),
         ("note", 2, 4, "entries stay PENDING in the consumer group"), (4, 2, "XREADGROUP id=0 (own pending) / XAUTOCLAIM"),
         (4, 4, "re-process; per-VIN seq guard drops repeats"), (4, 5, "INSERT alert ON CONFLICT DO NOTHING (no duplicate alert)"),
         (4, 2, "XACK / commit"), ("note", 2, 5, "chaos test: 278,061 accepted = 278,061 in lake, 0 lost, 0 duplicated")]
    return sequence("Sequence 2 - failure paths: duplicate, OEM format change, worker crash", A, m, 520)


def main():
    os.makedirs(OUT, exist_ok=True)
    diags = {"context": context(), "container": container(), "dataflow": dataflow(), "er": er(),
             "deployment": deployment(), "layers": layers(), "seq_alert": seq_alert(), "seq_fail": seq_fail()}
    with sync_playwright() as p:
        b = p.chromium.launch()
        for name, s in diags.items():
            open(os.path.join(OUT, f"diag_{name}.svg"), "w").write(s)
            pg = b.new_page(device_scale_factor=2)
            pg.set_content(f"<html><body style='margin:0'>{s}</body></html>")
            pg.locator("svg").screenshot(path=os.path.join(OUT, f"diag_{name}.png"))
        b.close()
    print("ok", list(diags))


if __name__ == "__main__":
    main()
