"""Streaming data structures used on the hot path.

* BloomFilter      - duplicate suppression of (vin, seq) keys.   O(k) per op, m bits.
* RotatingBloom    - time-bounded dedup window (two generations) so memory stays flat.
* CountMinSketch   - approximate DTC frequency for top-K.          O(d) per op, d*w counters.
* TopK             - heap of heavy hitters backed by CMS.           O(log K) per update.
* SlidingWindow    - per-vehicle time window mean/max/slope.        amortised O(1) per event.
* geohash_encode   - spatial bucketing of lat/lon.                  O(precision).
"""
from __future__ import annotations

import hashlib
import heapq
import math
from array import array
from collections import deque


def _hashes(key: bytes, k: int, m: int):
    # Kirsch-Mitzenmacher double hashing: h_i = h1 + i*h2
    d = hashlib.blake2b(key, digest_size=16).digest()
    h1 = int.from_bytes(d[:8], "little")
    h2 = int.from_bytes(d[8:], "little") | 1
    return [(h1 + i * h2) % m for i in range(k)]


class BloomFilter:
    def __init__(self, capacity: int, error_rate: float = 1e-3):
        self.m = max(8, int(-capacity * math.log(error_rate) / (math.log(2) ** 2)))
        self.k = max(1, round(self.m / capacity * math.log(2)))
        self.bits = bytearray((self.m + 7) // 8)
        self.count = 0

    def add(self, key: bytes) -> bool:
        """Adds key; returns True if it was (probably) already present."""
        present = True
        for h in _hashes(key, self.k, self.m):
            byte, bit = divmod(h, 8)
            if not self.bits[byte] & (1 << bit):
                present = False
                self.bits[byte] |= 1 << bit
        if not present:
            self.count += 1
        return present

    def __contains__(self, key: bytes) -> bool:
        return all(self.bits[h // 8] & (1 << (h % 8)) for h in _hashes(key, self.k, self.m))


class RotatingBloom:
    """Two-generation Bloom filter: a key is 'seen' if in current or previous
    generation. When current fills, previous is dropped. Bounded memory with a
    dedup horizon of >= `capacity` keys."""

    def __init__(self, capacity: int, error_rate: float = 1e-3):
        self.capacity, self.error_rate = capacity, error_rate
        self.cur = BloomFilter(capacity, error_rate)
        self.prev: BloomFilter | None = None

    def seen(self, key: bytes) -> bool:
        if self.prev is not None and key in self.prev:
            return True
        dup = self.cur.add(key)
        if self.cur.count >= self.capacity:
            self.prev, self.cur = self.cur, BloomFilter(self.capacity, self.error_rate)
        return dup


class CountMinSketch:
    def __init__(self, width: int = 2048, depth: int = 5):
        self.w, self.d = width, depth
        self.table = [array("l", [0] * width) for _ in range(depth)]

    def add(self, key: str, n: int = 1) -> int:
        est = None
        for row, h in zip(self.table, _hashes(key.encode(), self.d, self.w)):
            row[h] += n
            est = row[h] if est is None else min(est, row[h])
        return est

    def estimate(self, key: str) -> int:
        return min(row[h] for row, h in zip(self.table, _hashes(key.encode(), self.d, self.w)))


class TopK:
    def __init__(self, k: int = 10, sketch: CountMinSketch | None = None):
        self.k = k
        self.cms = sketch or CountMinSketch()
        self.best: dict[str, int] = {}

    def add(self, key: str) -> None:
        est = self.cms.add(key)
        if key in self.best or len(self.best) < self.k:
            self.best[key] = est
            return
        low_key = min(self.best, key=self.best.__getitem__)
        if est > self.best[low_key]:
            del self.best[low_key]
            self.best[key] = est

    def items(self) -> list[tuple[str, int]]:
        return heapq.nlargest(self.k, self.best.items(), key=lambda kv: kv[1])


class SlidingWindow:
    """Time-based window over (ts, value). Maintains running sum and a
    monotonic deque for max -> O(1) amortised add/evict."""

    __slots__ = ("span", "q", "maxq", "total")

    def __init__(self, span_s: float):
        self.span = span_s
        self.q: deque = deque()
        self.maxq: deque = deque()
        self.total = 0.0

    def add(self, ts: float, v: float) -> None:
        self.q.append((ts, v))
        self.total += v
        while self.maxq and self.maxq[-1][1] <= v:
            self.maxq.pop()
        self.maxq.append((ts, v))
        cutoff = ts - self.span
        while self.q and self.q[0][0] < cutoff:
            _, old = self.q.popleft()
            self.total -= old
        while self.maxq and self.maxq[0][0] < cutoff:
            self.maxq.popleft()

    @property
    def mean(self) -> float:
        return self.total / len(self.q) if self.q else 0.0

    @property
    def max(self) -> float:
        return self.maxq[0][1] if self.maxq else 0.0

    @property
    def slope(self) -> float:
        """Rate of change per hour between oldest and newest point."""
        if len(self.q) < 2:
            return 0.0
        (t0, v0), (t1, v1) = self.q[0], self.q[-1]
        return 0.0 if t1 == t0 else (v1 - v0) / (t1 - t0) * 3600.0


_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash_encode(lat: float, lon: float, precision: int = 6) -> str:
    lat_rng, lon_rng = [-90.0, 90.0], [-180.0, 180.0]
    out, bits, ch, even = [], 0, 0, True
    while len(out) < precision:
        rng, val = (lon_rng, lon) if even else (lat_rng, lat)
        mid = (rng[0] + rng[1]) / 2
        ch <<= 1
        if val >= mid:
            ch |= 1
            rng[0] = mid
        else:
            rng[1] = mid
        even = not even
        bits += 1
        if bits == 5:
            out.append(_B32[ch])
            bits, ch = 0, 0
    return "".join(out)
