# Infrastructure as code

* `aws/` – complete stack (VPC, EKS, MSK, RDS PostgreSQL, ElastiCache Redis, S3 lake with hot/warm/cold lifecycle, KMS)
  and the Helm release. `terraform init -backend-config=... && terraform apply -var env=staging`.
* Second cloud / on-prem: the application is the same Helm chart. Point `config.KAFKA_BROKERS`, `REDIS_URL`,
  `DATABASE_URL` (secret) and `LAKE_DIR` at the equivalents – GCP: GKE + Confluent/Managed Kafka + Cloud SQL +
  Memorystore + GCS; Azure: AKS + Event Hubs (Kafka API) + Flexible Server + Azure Cache + ADLS; local: `kind` +
  Strimzi + the compose images. No application code changes.
