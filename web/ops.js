"use strict";
const $ = (id) => document.getElementById(id);
let TOKEN, prev = null;
const hist = {eps: [], lag: []};
function line(xs, color) {
  const W = 560, H = 140, P = 48, n = xs.length, max = Math.max(1, ...xs);
  const d = xs.map((v, i) => `${i ? "L" : "M"}${(P + i * (W - 2 * P) / Math.max(1, n - 1)).toFixed(1)},${(H - P - v / max * (H - 2 * P)).toFixed(1)}`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" width="100%"><line x1="${P}" x2="${W - P}" y1="${H - P}" y2="${H - P}" stroke="#30363d"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="2"/><text x="${P - 4}" y="${P}" text-anchor="end">${Math.round(max).toLocaleString()}</text>
    <text x="${P}" y="${H - 8}">-${n * 2}s</text><text x="${W - P}" y="${H - 8}" text-anchor="end">now</text></svg>`;
}
function bars(xs) {
  const W = 560, H = 140, P = 48, max = Math.max(1, ...xs), bw = (W - 2 * P) / xs.length;
  return `<svg viewBox="0 0 ${W} ${H}" width="100%">${xs.map((v, i) => `<rect x="${P + i * bw + 3}" y="${H - P - v / max * (H - 2 * P)}" width="${bw - 6}" height="${v / max * (H - 2 * P)}" fill="#58a6ff" rx="2"/><text x="${P + i * bw + bw / 2}" y="${H - 8}" text-anchor="middle">p${i}</text>`).join("")}</svg>`;
}
async function tick() {
  const r = await (await fetch("/api/v1/ops/pipeline", {headers: {Authorization: `Bearer ${TOKEN}`}})).json();
  const processed = r.workers.reduce((a, w) => a + (w.processed || 0), 0);
  const lag = r.lag_per_partition.reduce((a, b) => a + b, 0);
  if (prev) {
    const eps = Math.max(0, (processed - prev.p) / (r.ts - prev.t));
    hist.eps.push(eps); hist.lag.push(lag);
    if (hist.eps.length > 90) { hist.eps.shift(); hist.lag.shift(); }
    $("eps").textContent = Math.round(eps).toLocaleString();
  }
  prev = {p: processed, t: r.ts};
  $("lag").textContent = lag.toLocaleString();
  $("alerts").textContent = r.workers.reduce((a, w) => a + (w.alerts || 0), 0).toLocaleString();
  $("p95").textContent = Math.max(0, ...r.workers.map((w) => w.alert_latency_p95_ms || 0)).toFixed(0);
  $("dups").textContent = r.workers.reduce((a, w) => a + (w.duplicate || 0) + (w.late || 0), 0).toLocaleString();
  $("c-eps").innerHTML = line(hist.eps, "#3fb950");
  $("c-lag").innerHTML = line(hist.lag, "#d29922");
  $("c-part").innerHTML = bars(r.lag_per_partition);
  $("w").innerHTML = `<table style="width:100%">${["worker", "partitions", "vehicles", "processed", "alerts", "p95 ms"].map((h) => `<th style="text-align:left" class="muted">${h}</th>`).join("")}${r.workers.map((w, i) => `<tr><td>#${i}</td><td>${w.partitions}</td><td>${Math.round(w.vehicles).toLocaleString()}</td><td>${Math.round(w.processed).toLocaleString()}</td><td>${w.alerts}</td><td>${w.alert_latency_p95_ms}</td></tr>`).join("")}</table>`;
  $("ts").textContent = new Date(r.ts * 1000).toLocaleTimeString();
}
$("login").onclick = async () => {
  const error = $("login-error");
  error.textContent = "";
  const r = await fetch("/auth/token", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({email: $("user").value, password: $("password").value})});
  if (!r.ok) { error.textContent = "Sign-in failed"; return; }
  TOKEN = (await r.json()).access_token;
  $("password").value = "";
  tick(); setInterval(tick, 2000);
};
