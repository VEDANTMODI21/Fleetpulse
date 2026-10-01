"""Ingest use case: raw OEM payload -> validated canonical event on the bus.

Guarantees
* Schema validation + VIN check digit + range checks (fail fast -> DLQ with reason).
* Idempotency: (vin, seq) de-duplicated with a rotating Bloom filter; the
  downstream consumer is also idempotent (seq high-water mark per VIN), so a
  Bloom false positive can never create a duplicate, only (rarely, p<=1e-3)
  drop a genuine event - an accepted, measured trade-off.
* Back-pressure: bounded in-flight buffer; `offer()` returns False when full
  so the HTTP/MQTT edge answers 429 / withholds PUBACK and the OEM retries.
"""
from __future__ import annotations

import time

import orjson

from fleetpulse.algorithms.streaming import RotatingBloom
from fleetpulse.domain.models import CanonicalEvent
from fleetpulse.ingest.bus import EventBus
from fleetpulse.ingest.normalizer import Normalizer, ValidationError

CANONICAL_TOPIC = "telemetry.canonical"
DLQ_TOPIC = "telemetry.dlq"


_FIELDS = CanonicalEvent.__slots__


def encode(ev: CanonicalEvent) -> bytes:
    return orjson.dumps({k: getattr(ev, k) for k in _FIELDS}, option=orjson.OPT_SERIALIZE_NUMPY)


def decode(b: bytes) -> CanonicalEvent:
    d = orjson.loads(b)
    d["dtcs"] = tuple(d.get("dtcs") or ())
    return CanonicalEvent(**d)


class IngestPipeline:
    def __init__(self, bus: EventBus, normalizer: Normalizer | None = None, dedup_capacity: int = 2_000_000,
                 max_buffer: int = 50_000, flush_size: int = 2_000):
        self.bus = bus
        self.norm = normalizer or Normalizer(time.time)
        self.dedup = RotatingBloom(dedup_capacity, 1e-3)
        self.buffer: list[tuple[str, bytes]] = []
        self.max_buffer, self.flush_size = max_buffer, flush_size
        self.stats = {"accepted": 0, "duplicate": 0, "invalid": 0, "rejected_backpressure": 0}

    def offer(self, oem: str, raw: dict) -> str:
        """Returns 'accepted' | 'duplicate' | 'invalid' | 'backpressure'."""
        if len(self.buffer) >= self.max_buffer:
            self.stats["rejected_backpressure"] += 1
            return "backpressure"
        try:
            ev = self.norm.normalize(oem, raw)
        except ValidationError as e:
            self.stats["invalid"] += 1
            self.bus.publish(DLQ_TOPIC, oem, orjson.dumps({"oem": oem, "error": str(e), "raw": raw},
                                                          option=orjson.OPT_NON_STR_KEYS, default=str))
            return "invalid"
        if self.dedup.seen(f"{ev.vin}:{ev.seq}".encode()):
            self.stats["duplicate"] += 1
            return "duplicate"
        self.buffer.append((ev.vin, encode(ev)))
        self.stats["accepted"] += 1
        if len(self.buffer) >= self.flush_size:
            self.flush()
        return "accepted"

    def flush(self) -> int:
        n = len(self.buffer)
        if n:
            self.bus.publish_many(CANONICAL_TOPIC, self.buffer)
            self.buffer = []
        return n
