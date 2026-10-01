# ADR-003: At-least-once delivery + idempotent sinks instead of exactly-once; CAP per data class

**Status:** accepted

**Context.** Duplicates come from OEM retries, simulator noise (1 %), and redelivery after a consumer crash. Kafka EOS
(transactions) covers Kafka-to-Kafka only. Our sinks are PostgreSQL, Redis and object storage.

**Decision.** At-least-once end to end, with idempotency at every step:
* ingest: rotating Bloom filter on (vin, seq), with a measured false-positive rate of 0.11 %;
* stream: a per-VIN `seq` high-water mark drops duplicate and late events before any state change;
* PostgreSQL: `alert.dedup_key UNIQUE` + `ON CONFLICT DO NOTHING`; work orders take an `Idempotency-Key`;
* offsets / XACK are committed only after the alerts are durable and the lake file is written.

**CAP / PACELC.** Billing, subscriptions, access control, work orders and audit are **PC/EC**: synchronous commit, and
we accept higher write latency. Raw telemetry, live state and caches are **PA/EL**: we serve slightly stale data rather
than block. A Redis outage opens the circuit breaker and the API keeps serving from PostgreSQL (tested).

**Consequences.** Simple and testable (the chaos test lost 0 and duplicated 0 of 278,061 events). A Bloom false positive
can drop a genuine event at p ≈ 0.1 %. That is accepted for telemetry; a crash-safe path for billing-grade events would use
an exact key set instead.
