"""Maintenance planning algorithms.

1. plan_service_slots  - 0/1 knapsack DP: choose which at-risk vehicles to pull
   into the workshop this week under a technician-hours budget, maximising
   expected avoided cost  sum(p_fail * breakdown_cost - service_cost).
   Time O(n * H), space O(n * H) for reconstruction (H = budget in 0.5h units).

2. nearest_depot       - multi-target Dijkstra on the road graph from the
   vehicle's node to the closest depot with free capacity.
   Time O((V + E) log V), space O(V).
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    vehicle_id: str
    p_fail: float          # model probability of breakdown in 7 days
    breakdown_cost: float  # tow + downtime + repair premium (USD)
    service_cost: float    # planned repair cost (USD)
    hours: float           # technician hours

    @property
    def value(self) -> float:
        return self.p_fail * self.breakdown_cost - self.service_cost


def plan_service_slots(cands: list[Candidate], budget_hours: float, unit: float = 0.5):
    items = [c for c in cands if c.value > 0]
    H = int(budget_hours / unit)
    w = [max(1, round(c.hours / unit)) for c in items]
    n = len(items)
    # dp[i][h] = best value using first i items with h units
    dp = [[0.0] * (H + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        wi, vi = w[i - 1], items[i - 1].value
        prev, cur = dp[i - 1], dp[i]
        for h in range(H + 1):
            best = prev[h]
            if wi <= h and prev[h - wi] + vi > best:
                best = prev[h - wi] + vi
            cur[h] = best
    chosen, h = [], H
    for i in range(n, 0, -1):
        if dp[i][h] != dp[i - 1][h]:
            chosen.append(items[i - 1])
            h -= w[i - 1]
    chosen.reverse()
    return chosen, dp[n][H]


def plan_greedy(cands: list[Candidate], budget_hours: float):
    """Baseline: value density greedy (used to show DP benefit in tests)."""
    items = sorted((c for c in cands if c.value > 0), key=lambda c: c.value / c.hours, reverse=True)
    out, used = [], 0.0
    for c in items:
        if used + c.hours <= budget_hours:
            out.append(c)
            used += c.hours
    return out, sum(c.value for c in out)


def nearest_depot(graph: dict[str, list[tuple[str, float]]], source: str, depots: dict[str, int]):
    """graph: node -> [(neighbour, km)], depots: node -> free slots.
    Returns (depot_node, distance_km, path) or None."""
    dist = {source: 0.0}
    prev: dict[str, str] = {}
    pq = [(0.0, source)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, float("inf")):
            continue
        if depots.get(u, 0) > 0:
            path = [u]
            while path[-1] != source:
                path.append(prev[path[-1]])
            return u, d, path[::-1]
        for v, km in graph.get(u, ()):
            nd = d + km
            if nd < dist.get(v, float("inf")):
                dist[v], prev[v] = nd, u
                heapq.heappush(pq, (nd, v))
    return None


def grid_graph(n: int, km: float = 1.0) -> dict[str, list[tuple[str, float]]]:
    g: dict[str, list[tuple[str, float]]] = {}
    for r in range(n):
        for c in range(n):
            u = f"{r},{c}"
            nb = []
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                rr, cc = r + dr, c + dc
                if 0 <= rr < n and 0 <= cc < n:
                    nb.append((f"{rr},{cc}", km * (1.0 + ((r * 7 + c * 13) % 5) / 10)))
            g[u] = nb
    return g
