# STRIDE threat model – ingestion path and public API

Scope: OEM cloud / device → ingress (mTLS) → ingest service → Kafka → stream workers → stores → public API → web UI / copilot.

| # | Threat (STRIDE) | Asset / entry point | Control implemented | Evidence |
|---|---|---|---|---|
| 1 | **Spoofing** a vehicle or OEM to inject fake telemetry | `/ingest/{oem}` | mTLS client certs per OEM at ingress (`auth-tls-verify-client`), HMAC-compared API key per OEM, VIN check-digit validation, range checks | `test_end_to_end_ingest_to_alert` (401 on bad key), `tests/unit/test_ingest.py` |
| 2 | **Tampering** with audit or alert history | `audit_log`, `alert` | Hash-chained audit, insert-only role, alerts immutable (dedup key), KMS-encrypted storage | `test_erasure_flow_and_audit_chain` detects a modified row |
| 3 | **Repudiation** of agent or user actions | work orders, erasure, copilot | Every mutation and every agent tool call is audited with actor = user or `agent:copilot<user>`; human approval required | BDD "Copilot proposes a work order…" |
| 4 | **Information disclosure** across tenants / precise location leakage | all read endpoints | JWT tenant claim enforced in code (`scope_fleet`), repo queries always filtered by fleet, analysts get coarse location (2 dp ≈ 1.1 km), TLS 1.3, AES-256 at rest | DAST-lite 14/14 (BOLA checks), `test_vehicle_detail_masks_location…` |
| 5 | **Denial of service** by bursts or large payloads | ingest, API | Bounded ingest buffer → 429 + Retry-After, 8 MB / 5,000-event batch cap, token-bucket per user (Redis Lua), page-size caps, ingress rate limit, HPA/KEDA | `test_ingest_backpressure_429`, `test_rate_limit_returns_429` |
| 6 | **Elevation of privilege** (forged JWT, alg=none, role escalation) | API | PyJWT with an explicit algorithm allow-list, required `exp/iat/aud/iss`, unknown roles rejected, permission checks per endpoint | `test_invalid_tokens`, `test_alg_none_rejected`, DAST |
| 7 | **Prompt injection** makes the agent exfiltrate data or act | `/agent/ask` | Pattern screening, tools scoped by principal, role tool allow-list, write tools only *propose*, grounding check on VINs, 4-tool budget | `test_prompt_injection_refused`, `test_grounding_check…` |
| 8 | **Supply chain** (malicious dependency / model file) | build, model registry | Pinned requirements, pip-audit (0 known vulns), Trivy image scan in CI, SBOM, SHA-256 verified model artefacts | `docs/security/pip-audit.json`, `fleetpulse/ml/registry.py` |

Top residual risks: (1) the local shared-secret login (`/auth/token`) must be replaced by OIDC before any real
deployment. (2) Pattern-based injection screening can be bypassed; the real protection is tool scoping and human approval.
(3) The Bloom dedup can drop about 0.1 % of genuine events.
