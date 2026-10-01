# Algorithms & SQL write-up

All timings measured on the 2-vCPU benchmark host (`python -m bench.algorithms_bench` → `bench/results/algorithms.json`).

| Problem | Algorithm | Time / space | Scale tested | Measured |
|---|---|---|---|---|
| VIN validation | regex `^[A-HJ-NPR-Z0-9]{17}$` + ISO 3779 weighted check digit (mod 11) | O(1) / O(1) | 200K valid + 100K corrupted | 516K VIN/s, 100 % of bad check digits rejected |
| DTC parsing from heterogeneous payloads | single-pass regex for `P0301`, `p03-01`, `DTC=…;…` + J1979 2-byte hex decoding | O(n) in payload length | 200K payloads | 177K payloads/s |
| Duplicate suppression | rotating 2-generation Bloom filter, Kirsch–Mitzenmacher double hashing, k = 10 | O(k) per op, 1.8 MB per 1M keys | 1M keys | 223K inserts/s, FP 0.113 % (target 0.1 %) |
| Top-K DTCs | Count-Min Sketch (5×2048) + heavy-hitter map | O(d) per update, 10,240 counters | 500K Zipf codes | top-10 exact match 10/10 |
| Rolling signal windows | time-based deque + monotonic max-deque | amortised O(1) | 1M points | 1.8M updates/s |
| Spatial bucketing | geohash (base-32 interleaving) | O(precision) | 200K | 186K/s |
| Workshop selection | 0/1 knapsack DP over technician hours (0.5 h units) | O(n·H) time/space | 500 candidates, 30 h | ~1 ms; optimal; greedy was 0.05 % worse here and 78 % worse on the adversarial unit-test case |
| Routing to the nearest depot with a free bay | multi-target Dijkstra with binary heap | O((V+E) log V) | 90K-node grid, 40 depots | 4.6 ms/query |

## Pseudocode

```
plan_service_slots(C, budget):            # value = p_fail*breakdown_cost - service_cost
  items = [c in C if value(c) > 0]; H = budget/0.5
  dp[0..n][0..H] = 0
  for i in 1..n: for h in 0..H:
      dp[i][h] = max(dp[i-1][h], dp[i-1][h-w_i] + v_i  if w_i <= h)
  backtrack from dp[n][H] -> chosen set

nearest_depot(G, s, free):
  dist[s]=0; heap=[(0,s)]
  while heap: (d,u)=pop; if d>dist[u]: continue
      if free[u]>0: return u, d, path(u)
      for (v,w) in G[u]: relax(u,v,w)

StreamProcessor.process(ev):              # O(1) amortised
  st = state[vin]; if ev.seq <= st.last_seq: drop (duplicate/late)
  update windows; for rule in rules: if fires and not debounced(kind, 10 min): emit alert
```

## SQL optimisation (EXPLAIN ANALYZE, full plans in `docs/query_plans/`)

| Query | Before | After | Change |
|---|---|---|---|
| Q1 top-50 at-risk vehicles of a fleet with open-alert counts | 306 ms | 5.7 ms | composite `risk_score(scored_on, p_fail_7d DESC) INCLUDE (vehicle_id)`, `vehicle(fleet_id)`, **partial** index `alert(vehicle_id) WHERE status='OPEN'`; correlated sub-query (N+1 pattern) turned into index lookups |
| Q2 alert list, page 400 (`OFFSET 20000`) | 19.3 ms | 0.05 ms | keyset pagination on `(ts, id)` + composite index `(severity, ts DESC, id DESC)`; cost no longer grows with page number |
| Q3 30-day fleet trend over 3M rollup rows | 561 ms | 0.05 ms | materialised view `fleet_daily_summary` (unique index, refreshed CONCURRENTLY by the nightly job) |
