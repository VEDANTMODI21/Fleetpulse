"use strict";
const $ = (id) => document.getElementById(id);
let TOKEN = null, CURSOR = null, ES = null;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(path, opts = {}) {
  const r = await fetch(path, {...opts, headers: {"Content-Type": "application/json", Authorization: `Bearer ${TOKEN}`, ...(opts.headers || {})}});
  if (!r.ok) throw new Error((await r.json()).title || r.status);
  return r.json();
}
const riskColor = (p) => p >= 0.5 ? "var(--hi)" : p >= 0.15 ? "var(--med)" : "var(--lo)";

async function login() {
  const error = $("login-error");
  error.textContent = "";
  const r = await fetch("/auth/token", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({email: $("user").value, password: $("password").value})});
  if (!r.ok) { error.textContent = "Sign-in failed"; return; }
  TOKEN = (await r.json()).access_token;
  $("password").value = "";
  CURSOR = null; $("risk").innerHTML = "";
  await Promise.all([loadSummary(), loadRisk()]);
  live();
}

async function loadSummary() {
  const s = await api("/api/v1/fleet/summary");
  $("k-veh").textContent = s.risk.total.toLocaleString();
  $("k-high").textContent = s.risk.high.toLocaleString();
  $("k-med").textContent = s.risk.medium.toLocaleString();
  $("k-alerts").textContent = s.open_alerts.toLocaleString();
  $("k-brk").textContent = s.trend.reduce((a, d) => a + Number(d.breakdowns), 0).toLocaleString();
  $("trend").innerHTML = chart(s.trend.map((d) => ({x: d.day, a: Number(d.breakdowns), b: Number(d.severe_dtcs)})));
}

function chart(rows) {
  if (!rows.length) return "";
  const W = 1100, H = 150, P = 28, max = Math.max(...rows.map((r) => Math.max(r.a, r.b)), 1);
  const x = (i) => P + i * (W - 2 * P) / Math.max(1, rows.length - 1), y = (v) => H - P - v / max * (H - 2 * P);
  const line = (k) => rows.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(r[k]).toFixed(1)}`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Daily breakdowns and severe DTCs">
    <line x1="${P}" x2="${W - P}" y1="${H - P}" y2="${H - P}" stroke="#e3e7ec"/>
    <path d="${line("b")}" fill="none" stroke="#bf8700" stroke-width="2"/>
    <path d="${line("a")}" fill="none" stroke="#d1242f" stroke-width="2"/>
    <text x="${P}" y="${H - 8}">${esc(rows[0].x)}</text><text x="${W - P}" y="${H - 8}" text-anchor="end">${esc(rows.at(-1).x)}</text>
    <text x="${W - P}" y="${y(rows.at(-1).a) - 6}" text-anchor="end" style="fill:#d1242f">breakdowns</text>
    <text x="${W - P}" y="${y(rows.at(-1).b) - 6}" text-anchor="end" style="fill:#bf8700">severe DTCs</text>
    <text x="${P - 4}" y="${y(max) + 4}" text-anchor="end">${max}</text></svg>`;
}

async function loadRisk() {
  const q = CURSOR ? `?limit=25&cursor=${encodeURIComponent(CURSOR)}` : "?limit=25";
  const d = await api("/api/v1/vehicles/at-risk" + q);
  $("scored").textContent = `· scored ${d.scored_on}`;
  CURSOR = d.next_cursor;
  $("risk").insertAdjacentHTML("beforeend", d.items.map((v) => `
    <tr class="row" data-vin="${esc(v.vin)}"><td><code>${esc(v.vin)}</code></td>
    <td>${esc(v.oem)} ${esc(v.powertrain)} ${esc(v.model_year)}</td><td>${esc(v.city)}</td>
    <td><span class="bar"><i style="width:${(v.p_fail_7d * 100).toFixed(0)}%;background:${riskColor(v.p_fail_7d)}"></i></span>${(v.p_fail_7d * 100).toFixed(0)}%</td>
    <td class="small">${esc(v.top_factors[0]?.factor ?? "—")}</td><td>${v.open_alerts}</td></tr>`).join(""));
}

async function openVehicle(vin) {
  const v = await api(`/api/v1/vehicles/${vin}`);
  const r = v.risk_history.at(-1) || {p_fail_7d: 0, top_factors: []};
  const spark = (k, label) => {
    const pts = v.daily.map((d) => d[k]).filter((z) => z !== null);
    if (!pts.length) return "";
    const mn = Math.min(...pts), mx = Math.max(...pts), W = 240, H = 40;
    const path = pts.map((p, i) => `${i ? "L" : "M"}${(i * W / Math.max(1, pts.length - 1)).toFixed(1)},${(H - (p - mn) / ((mx - mn) || 1) * H).toFixed(1)}`).join("");
    return `<div class="small">${label}: ${pts.at(-1).toFixed(1)} <span class="muted">(30-day ${mn.toFixed(1)}–${mx.toFixed(1)})</span><br>
      <svg width="${W}" height="${H + 4}"><path d="${path}" fill="none" stroke="#1f6feb" stroke-width="1.5"/></svg></div>`;
  };
  $("drawer").innerHTML = `<button class="b g" id="close">Close</button>
    <h2 style="margin:12px 0 2px">${esc(v.vin)}</h2><div class="muted">${esc(v.oem)} ${esc(v.powertrain)} ${esc(v.model_year)} · ${esc(v.depot)}</div>
    <h3 style="color:${riskColor(r.p_fail_7d)}">${(r.p_fail_7d * 100).toFixed(0)}% risk of breakdown in 7 days</h3>
    <ul class="factors">${r.top_factors.map((f) => `<li>${esc(f.factor)} <span class="muted">(${esc(f.signal)} = ${f.value})</span></li>`).join("") || "<li>No abnormal signals</li>"}</ul>
    <p><button class="b" id="wo">Propose work order</button> <span id="wores" class="small"></span></p>
    ${spark("coolant_max", "Coolant max °C")}${spark("aux_v_min", "12V min V")}${spark("oil_kpa_min", "Oil pressure min kPa")}
    ${spark("misfire_cnt", "Misfires / day")}${spark("cell_temp_spread", "HV cell temp spread °C")}
    <h3>Live state</h3><div class="small">${v.live.available ? `Speed ${v.live.speed_kmh.toFixed(0)} km/h · coolant 15-min mean ${v.live.coolant_mean_15m} °C · 12V ${v.live.aux_v_mean_15m} V · location ${v.live.lat}, ${v.live.lon}` : "No live telemetry in the last hour"}</div>
    <h3>Recent alerts</h3>${v.alerts.map((a) => `<div class="small"><span class="pill ${esc(a.severity)}">${esc(a.kind)}</span> ${esc(a.ts.slice(0, 16))} ${esc(a.detail)} <span class="muted">${esc(a.status)}</span></div>`).join("") || "<div class='muted'>None</div>"}
    <h3>Work orders</h3><div id="wos">${v.work_orders.map(woRow).join("") || "<span class='muted'>None</span>"}</div>`;
  $("drawer").classList.add("open");
  $("close").onclick = () => $("drawer").classList.remove("open");
  $("wo").onclick = async () => {
    const reason = r.top_factors.map((f) => f.factor).join("; ") || "Preventive inspection";
    try {
      const w = await api("/api/v1/work-orders", {method: "POST", headers: {"Idempotency-Key": `${vin}-${new Date().toISOString().slice(0, 10)}`},
        body: JSON.stringify({vin, reason})});
      $("wores").textContent = `Work order #${w.id} ${w.replayed ? "(already existed)" : "created"} – ${w.status}`;
    } catch (e) { $("wores").textContent = e.message; }
  };
  document.querySelectorAll("[data-approve]").forEach((b) => b.onclick = async () => {
    try { const x = await api(`/api/v1/work-orders/${b.dataset.approve}/approve`, {method: "POST"}); b.replaceWith(x.status); }
    catch (e) { b.replaceWith(e.message); }
  });
}
const woRow = (w) => `<div class="small">#${w.id} <b>${esc(w.status)}</b> ${esc(w.reason)} <span class="muted">by ${esc(w.proposed_by)}</span>
  ${w.status === "PROPOSED" ? `<button class="b g" data-approve="${w.id}">Approve</button>` : ""}</div>`;

function live() {
  if (ES) ES.close();
  ES = new EventSource(`/api/v1/stream/alerts?token=${encodeURIComponent(TOKEN)}`);
  let first = true;
  ES.onmessage = (m) => {
    const a = JSON.parse(m.data);
    if (first) { $("feed").innerHTML = ""; first = false; }
    const lat = ((Date.now() / 1000 - a.ts) * 1000).toFixed(0);
    $("feed").insertAdjacentHTML("afterbegin", `<div class="new"><span class="pill ${esc(a.severity)}">${esc(a.kind)}</span>
      <code>${esc(a.vin)}</code> ${esc(a.detail)} <span class="muted">· ${lat} ms after event</span></div>`);
    while ($("feed").children.length > 50) $("feed").lastChild.remove();
  };
}

async function plan() {
  const d = await api(`/api/v1/maintenance/plan?budget_hours=${Number($("hours").value) || 40}`);
  $("planout").innerHTML = `<p><b>${d.chosen.length}</b> vehicles · ${d.hours_used} h · expected avoided cost <b>USD ${Math.round(d.expected_saving).toLocaleString()}</b></p>` +
    `<table><tr><th>VIN</th><th>Risk</th><th>Hours</th><th>Depot</th></tr>${d.chosen.slice(0, 12).map((c) =>
      `<tr><td><code>${esc(c.vin)}</code></td><td>${(c.p_fail * 100).toFixed(0)}%</td><td>${c.hours}</td><td>${esc(c.depot_city ?? "–")}${c.route_km ? ` (${c.route_km} km)` : ""}</td></tr>`).join("")}</table>`;
}

async function ask(q) {
  const log = $("chatlog");
  if (log.classList.contains("muted")) { log.textContent = ""; log.classList.remove("muted"); }
  log.insertAdjacentHTML("beforeend", `<div class="q">${esc(q)}</div>`);
  try {
    const r = await api("/api/v1/agent/ask", {method: "POST", body: JSON.stringify({question: q})});
    log.insertAdjacentHTML("beforeend", `<div>${esc(r.answer)}</div><div class="muted small">tools: ${esc(r.tool_calls.map((t) => t.tool).join(", ") || "none")} · ${r.latency_ms} ms${r.refused ? " · refused by guardrail" : ""}</div>`);
  } catch (e) { log.insertAdjacentHTML("beforeend", `<div>${esc(e.message)}</div>`); }
  log.scrollTop = log.scrollHeight;
}

$("login").onclick = login;
$("more").onclick = loadRisk;
$("plan").onclick = plan;
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.value.trim()) { ask(e.target.value.trim()); e.target.value = ""; } });
$("risk").addEventListener("click", (e) => { const tr = e.target.closest("tr.row"); if (tr) openVehicle(tr.dataset.vin); });
