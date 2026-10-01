output "cluster_name" { value = module.eks.cluster_name }
output "kafka_bootstrap" { value = aws_msk_cluster.kafka.bootstrap_brokers_tls }
output "pg_endpoint" { value = aws_db_instance.pg.address }
output "redis_endpoint" { value = aws_elasticache_replication_group.redis.primary_endpoint_address }
output "lake_bucket" { value = aws_s3_bucket.lake.bucket }
