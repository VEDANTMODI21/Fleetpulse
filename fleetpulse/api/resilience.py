"""Resilience primitives: token-bucket rate limiter and circuit breaker."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    """Per-key token bucket. O(1) per request. Redis Lua variant used when
    REDIS_URL is set so limits hold across API replicas."""

    LUA = """
    local k=KEYS[1]; local rate=tonumber(ARGV[1]); local cap=tonumber(ARGV[2]); local now=tonumber(ARGV[3])
    local b=redis.call('HMGET',k,'t','ts'); local t=tonumber(b[1]) or cap; local ts=tonumber(b[2]) or now
    t=math.min(cap, t+(now-ts)*rate); local ok=0
    if t>=1 then t=t-1; ok=1 end
    redis.call('HMSET',k,'t',t,'ts',now); redis.call('EXPIRE',k,math.ceil(cap/rate)+1)
    return ok"""

    def __init__(self, rate_per_s: float, capacity: int, redis_client=None, clock: Callable[[], float] = time.monotonic):
        self.rate, self.cap, self.clock = rate_per_s, capacity, clock
        self.r = redis_client
        self._script = redis_client.register_script(self.LUA) if redis_client is not None else None
        self._b: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self.degraded = 0

    def allow(self, key: str) -> bool:
        if self._script is not None:
            try:
                return bool(self._script(keys=[f"rl:{key}"], args=[self.rate, self.cap, time.time()]))
            except Exception:  # noqa: BLE001
                self.degraded += 1   # degrade to local limiting if Redis is unavailable
        now = self.clock()
        with self._lock:
            tokens, ts = self._b.get(key, (self.cap, now))
            tokens = min(self.cap, tokens + (now - ts) * self.rate)
            ok = tokens >= 1
            self._b[key] = (tokens - 1 if ok else tokens, now)
            return ok


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    """CLOSED -> (failures >= threshold) -> OPEN -> (after reset_s) -> HALF_OPEN -> success -> CLOSED."""

    def __init__(self, threshold: int = 5, reset_s: float = 10.0, clock: Callable[[], float] = time.monotonic):
        self.threshold, self.reset_s, self.clock = threshold, reset_s, clock
        self.failures, self.state, self.opened_at = 0, "CLOSED", 0.0

    def call(self, fn, *a, **kw):
        if self.state == "OPEN":
            if self.clock() - self.opened_at >= self.reset_s:
                self.state = "HALF_OPEN"
            else:
                raise CircuitOpen()
        try:
            out = fn(*a, **kw)
        except Exception:
            self.failures += 1
            if self.state == "HALF_OPEN" or self.failures >= self.threshold:
                self.state, self.opened_at = "OPEN", self.clock()
            raise
        self.failures, self.state = 0, "CLOSED"
        return out
