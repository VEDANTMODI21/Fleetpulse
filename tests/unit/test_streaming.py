from fleetpulse.algorithms.streaming import (
    BloomFilter,
    CountMinSketch,
    RotatingBloom,
    SlidingWindow,
    TopK,
    geohash_encode,
)


def test_bloom_no_false_negatives_and_low_fp():
    b = BloomFilter(10_000, 1e-3)
    for i in range(10_000):
        assert b.add(f"k{i}".encode()) in (False, True)
    assert all(f"k{i}".encode() in b for i in range(10_000))
    fp = sum(f"x{i}".encode() in b for i in range(20_000)) / 20_000
    assert fp < 5e-3


def test_bloom_add_reports_duplicate():
    b = BloomFilter(100)
    assert b.add(b"a") is False
    assert b.add(b"a") is True


def test_rotating_bloom_bounded_and_remembers_previous_generation():
    r = RotatingBloom(100)
    for i in range(150):
        r.seen(f"k{i}".encode())
    assert r.seen(b"k10")            # in previous generation
    assert r.prev is not None and r.cur.count <= 100


def test_count_min_never_underestimates():
    c = CountMinSketch(256, 4)
    for i in range(1000):
        c.add(f"k{i % 50}")
    assert all(c.estimate(f"k{i}") >= 20 for i in range(50))


def test_topk_finds_heavy_hitters():
    t = TopK(3)
    for k, n in (("A", 100), ("B", 50), ("C", 30), ("D", 5), ("E", 1)):
        for _ in range(n):
            t.add(k)
    assert [k for k, _ in t.items()] == ["A", "B", "C"]


def test_sliding_window_mean_max_slope_and_eviction():
    w = SlidingWindow(10)
    for t, v in [(0, 1), (5, 3), (9, 2)]:
        w.add(t, v)
    assert w.mean == 2 and w.max == 3
    w.add(16, 10)                  # evicts t=0 and t=5
    assert len(w.q) == 2 and w.max == 10 and w.mean == 6
    assert w.slope == (10 - 2) / 7 * 3600
    assert SlidingWindow(5).mean == 0 and SlidingWindow(5).max == 0 and SlidingWindow(5).slope == 0


def test_geohash_known_value():
    assert geohash_encode(57.64911, 10.40744, 11) == "u4pruydqqvj"
    assert geohash_encode(12.9716, 77.5946, 5) == "tdr1v"
