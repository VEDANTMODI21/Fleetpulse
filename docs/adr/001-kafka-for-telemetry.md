# ADR-001: Kafka as the telemetry backbone (Redis Streams for local/CI)

**Status:** accepted

**Context.** Ingest 100K+ events/s with 3x bursts and no data loss. Per-vehicle ordering is required so rules and sequence
guards work. The stream must be replayable, so a new consumer (for example a new model) can reprocess history and the DLQ
can be replayed after an OEM onboarding fix.

**Options.** (1) RabbitMQ: good for routing work queues, but messages are gone once consumed and replay is not native;
partitioned ordering needs a consistent-hash exchange. (2) Kafka: a partitioned log with retention, replay, consumer
groups, idempotent producers and huge throughput per broker. (3) Cloud-native (Kinesis / Pub/Sub): managed but
single-vendor, which the AWS IoT FleetWise shutdown (April 2026) showed is a risk.

**Decision.** Kafka (MSK / Strimzi / Confluent), key = VIN, 48 partitions for 100K ev/s (about 2–3K ev/s per partition,
the measured single-core stream worker capacity is ~65K ev/s, which leaves plenty of headroom). Code depends on an
`EventBus` port. `KafkaBus` is used in production, `RedisStreamsBus` in local dev and CI (Kafka images could not be pulled
in the benchmark sandbox), and `InMemoryBus` in unit tests.

**Consequences.** Replay and back-pressure (consumer lag) come for free, and KEDA scales workers on lag. Kafka is costly to
operate, so we use a managed service in the cloud. The Redis Streams adapter has the same at-least-once semantics
(pending entries list + XAUTOCLAIM), and the chaos test ran on it. Kafka-specific integration tests still run only in
the compose CI job.
