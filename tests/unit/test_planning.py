import itertools

from fleetpulse.algorithms.planning import Candidate, grid_graph, nearest_depot, plan_greedy, plan_service_slots


def brute(cands, budget):
    best = 0
    for r in range(len(cands) + 1):
        for combo in itertools.combinations([c for c in cands if c.value > 0], r):
            if sum(c.hours for c in combo) <= budget:
                best = max(best, sum(c.value for c in combo))
    return best


def test_knapsack_matches_brute_force():
    cands = [Candidate(f"V{i}", p, 3000, 300, h) for i, (p, h) in
             enumerate([(0.9, 3), (0.8, 2), (0.7, 2), (0.5, 1), (0.3, 1.5), (0.95, 4), (0.05, 1)])]
    chosen, val = plan_service_slots(cands, 5)
    assert abs(val - brute(cands, 5)) < 1e-6
    assert sum(c.hours for c in chosen) <= 5


def test_dp_beats_greedy_on_adversarial_case():
    # greedy by density takes the small item and cannot fit the big one
    cands = [Candidate("small", 1.0, 1100, 0, 1), Candidate("big", 1.0, 5000, 0, 5)]
    _, dp = plan_service_slots(cands, 5)
    _, gr = plan_greedy(cands, 5)
    assert dp == 5000 and gr == 1100


def test_negative_value_candidates_skipped():
    chosen, val = plan_service_slots([Candidate("x", 0.01, 100, 500, 1)], 10)
    assert chosen == [] and val == 0


def test_dijkstra_nearest_free_depot():
    g = grid_graph(5)
    hit = nearest_depot(g, "0,0", {"0,2": 1, "4,4": 1})
    assert hit[0] == "0,2" and hit[2][0] == "0,0" and hit[2][-1] == "0,2"
    hit = nearest_depot(g, "0,0", {"0,2": 0, "4,4": 1})      # full depot skipped
    assert hit[0] == "4,4"
    assert nearest_depot(g, "0,0", {}) is None
    assert nearest_depot(g, "0,2", {"0,2": 2})[1] == 0
