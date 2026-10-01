# ADR-002: Polyglot persistence – the right store per data class

**Status:** accepted

| Data | Store | Why | CAP |
|---|---|---|---|
| Fleets, vehicles, users, subscriptions, work orders, alerts, risk scores, audit | PostgreSQL 16 (3NF) | FK integrity, transactions, RLS-ready, rich SQL, partitioning | CP (sync standby in prod) |
| Daily rollups (hot 30–90 d) | PostgreSQL, partitioned by month | dashboard trends with partition pruning plus a materialised view | CP |
| Live vehicle state, cache, rate-limit buckets, alert fan-out | Redis | sub-ms reads, TTL, pub/sub, Lua for atomic limits | AP (async replica; state is rebuildable from the stream) |
| Raw canonical telemetry (billions of rows) | Parquet on object storage (Iceberg-ready), queried by DuckDB/Spark | cheap columnar scans, hot/warm/cold lifecycle | AP / eventually consistent |
| Repair knowledge embeddings | pgvector (HNSW) | keeps RAG data inside the tenant DB boundary; no extra vendor | CP |

**Rejected.** Everything in PostgreSQL: at 100K inserts/s B-tree write amplification and vacuum would dominate (see §4.1
of the brief). Cassandra/Scylla for raw telemetry: a strong fit for point reads by (vin, day), but our access pattern for
raw data is analytical scans, so the columnar lake is cheaper. A dedicated vector DB (Qdrant): unnecessary at a KB of
thousands of chunks.

**Consequences.** Three technologies to operate. Every cross-store write path is idempotent, so no distributed
transactions are needed.
