# ADR-005: Tamper-evident audit log with group commit for reads

**Status:** accepted

**Context.** DPDP/GDPR require an audit trail of every data access and every AI-agent action. Writing one synchronous
chained row per API read serialised requests on an advisory lock. In the first load test p95 reached 600 ms at 32
concurrent clients.

**Decision.** Each audit row stores `sha256(prev_hash || record)`, and `/audit/verify` walks the chain. The integration
test flips one field and verification fails. Mutations, erasure, approvals and agent tool calls are appended
synchronously in the request. High-volume read events go to an in-process queue that one transaction per 100 ms chains
and inserts (group commit). If the insert fails, the entries are put back on the queue.

**Consequences.** A pod crash can lose at most 100 ms of read-access records. Actions are never lost. In production the
DB role has INSERT-only rights on `audit_log`, and daily chain heads are anchored in WORM object storage (S3 Object Lock),
so rewriting the whole chain is also detectable.
