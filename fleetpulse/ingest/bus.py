"""EventBus port + adapters (hexagonal). Pipeline code depends only on EventBus.

* InMemoryBus     - unit tests / single-process demo.
* RedisStreamsBus - local dev & CI (partitioned streams: <topic>.<p>, consumer groups, replay by id).
* KafkaBus        - production (confluent-kafka; idempotent producer, acks=all). Selected with BUS=kafka.
Partition = crc32(key) % partitions, so all events of one VIN land in one partition (ordering per vehicle).
"""
from __future__ import annotations

import os
import time
import zlib
from collections import defaultdict, deque
from collections.abc import Iterable
from typing import Protocol


def partition_for(key: str, partitions: int) -> int:
    return zlib.crc32(key.encode()) % partitions


class EventBus(Protocol):
    partitions: int

    def publish(self, topic: str, key: str, value: bytes) -> None: ...
    def publish_many(self, topic: str, items: Iterable[tuple[str, bytes]]) -> None: ...
    def poll(self, topic: str, partition: int, max_n: int = 500) -> list[tuple[str, bytes]]: ...
    def lag(self, topic: str) -> int: ...
    def commit(self, topic: str, partition: int | None = None) -> None: ...


class InMemoryBus:
    def __init__(self, partitions: int = 8):
        self.partitions = partitions
        self.q: dict[tuple[str, int], deque] = defaultdict(deque)

    def publish(self, topic, key, value):
        self.q[(topic, partition_for(key, self.partitions))].append((key, value))

    def publish_many(self, topic, items):
        for k, v in items:
            self.publish(topic, k, v)

    def poll(self, topic, partition, max_n=500):
        q = self.q[(topic, partition)]
        return [q.popleft() for _ in range(min(max_n, len(q)))]

    def lag(self, topic):
        return sum(len(q) for (t, _), q in self.q.items() if t == topic)

    def commit(self, topic, partition=None):
        return None


class RedisStreamsBus:
    def __init__(self, url: str | None = None, partitions: int = 8, group: str = "stream-processor",
                 maxlen: int = 2_000_000):
        import redis
        self.r = redis.Redis.from_url(url or os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        self.partitions, self.group, self.maxlen = partitions, group, maxlen
        self.consumer = os.environ.get("HOSTNAME", "local")
        self.claim_idle_ms = int(os.environ.get("CLAIM_IDLE_MS", "15000"))
        self._groups: set[str] = set()
        self._recovered: set[str] = set()
        self._pending: dict[str, list] = {}
        self._claim_cursor: dict[str, str] = {}
        self._last_claim: dict[str, float] = {}

    def _stream(self, topic, p):
        return f"{topic}.{p}"

    def _ensure(self, s):
        if s not in self._groups:
            try:
                self.r.xgroup_create(s, self.group, id="0", mkstream=True)
            except Exception as e:  # BUSYGROUP
                if "BUSYGROUP" not in str(e):
                    raise
            self._groups.add(s)

    def publish(self, topic, key, value):
        self.publish_many(topic, [(key, value)])

    def publish_many(self, topic, items):
        pipe = self.r.pipeline(transaction=False)
        for k, v in items:
            pipe.xadd(self._stream(topic, partition_for(k, self.partitions)), {"k": k, "v": v},
                      maxlen=self.maxlen, approximate=True)
        pipe.execute()

    def poll(self, topic, partition, max_n=500):
        """At-least-once: entries stay in the group's pending list until `commit()` is called
        after processing. On (re)start a consumer first re-reads its own pending entries and
        claims entries left pending by dead consumers for longer than `claim_idle_ms`."""
        s = self._stream(topic, partition)
        self._ensure(s)
        entries = []
        now = time.monotonic()
        if s not in self._recovered or now - self._last_claim.get(s, 0) > self.claim_idle_ms / 1000:
            # 1) own pending entries (this pod restarted), 2) entries pending on dead consumers
            if s not in self._recovered:
                own = self.r.xreadgroup(self.group, self.consumer, {s: "0"}, count=max_n)
                for _, e in own or []:
                    entries += [x for x in e if x[1]]
                if len(entries) < max_n:
                    self._recovered.add(s)
            if not entries:
                nxt, claimed, *_ = self.r.xautoclaim(s, self.group, self.consumer, self.claim_idle_ms,
                                                     self._claim_cursor.get(s, "0-0"), count=max_n)
                entries += claimed
                self._claim_cursor[s] = nxt
                if nxt in (b"0-0", "0-0"):
                    self._last_claim[s] = now
        if not entries:
            res = self.r.xreadgroup(self.group, self.consumer, {s: ">"}, count=max_n)
            for _, e in res or []:
                entries += e
        out = []
        seen = set()
        for eid, f in entries:
            if eid in seen or not f:
                continue
            seen.add(eid)
            self._pending.setdefault(s, []).append(eid)
            out.append((f[b"k"].decode(), f[b"v"]))
        return out

    def commit(self, topic, partition=None):
        parts = range(self.partitions) if partition is None else [partition]
        for p in parts:
            s = self._stream(topic, p)
            ids = self._pending.pop(s, None)
            if ids:
                self.r.xack(s, self.group, *ids)

    def lag(self, topic):
        total = 0
        for p in range(self.partitions):
            s = self._stream(topic, p)
            self._ensure(s)
            for g in self.r.xinfo_groups(s):
                if g["name"].decode() == self.group:
                    total += g.get("lag") or 0
        return total


class KafkaBus:  # pragma: no cover - exercised in docker-compose / k8s, not in unit tests
    def __init__(self, brokers: str | None = None, partitions: int = 48, group: str = "stream-processor"):
        from confluent_kafka import Consumer, Producer
        self.partitions = partitions
        brokers = brokers or os.environ.get("KAFKA_BROKERS", "kafka:9092")
        self.p = Producer({"bootstrap.servers": brokers, "enable.idempotence": True, "acks": "all",
                           "linger.ms": 5, "compression.type": "zstd", "batch.num.messages": 10000})
        self.c = Consumer({"bootstrap.servers": brokers, "group.id": group, "enable.auto.commit": False,
                           "auto.offset.reset": "earliest"})
        self._subscribed: set[str] = set()

    def publish(self, topic, key, value):
        while True:
            try:
                self.p.produce(topic, value=value, key=key)
                break
            except BufferError:              # back-pressure: local queue full -> wait for delivery
                self.p.poll(0.05)
        self.p.poll(0)

    def publish_many(self, topic, items):
        for k, v in items:
            self.publish(topic, k, v)
        self.p.flush(5)

    def poll(self, topic, partition, max_n=500):
        if topic not in self._subscribed:
            self.c.subscribe([topic])
            self._subscribed.add(topic)
        msgs = self.c.consume(max_n, timeout=0.2)
        return [(m.key().decode(), m.value()) for m in msgs if m.error() is None]

    def commit(self, topic, partition=None):
        self.c.commit(asynchronous=False)   # offsets committed only after processing (at-least-once)

    def lag(self, topic):
        return -1


def bus_from_env() -> EventBus:
    kind = os.environ.get("BUS", "memory")
    parts = int(os.environ.get("PARTITIONS", "8"))
    if kind == "kafka":
        return KafkaBus(partitions=parts)
    if kind == "redis":
        return RedisStreamsBus(partitions=parts)
    return InMemoryBus(parts)
