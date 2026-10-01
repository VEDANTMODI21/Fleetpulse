# FleetPulse on AWS: VPC (3 AZ) + EKS + MSK (Kafka) + RDS PostgreSQL + ElastiCache Redis + S3 lake + KMS.
# The application layer is the cloud-agnostic Helm chart; only managed-service endpoints differ per cloud.
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws  = { source = "hashicorp/aws", version = "~> 5.60" }
    helm = { source = "hashicorp/helm", version = "~> 2.14" }
  }
  backend "s3" {}   # bucket/key/region/dynamodb_table supplied via -backend-config in CI
}

provider "aws" { region = var.region }

locals {
  name = "fleetpulse-${var.env}"
  tags = { project = "fleetpulse", env = var.env, owner = "platform" }
}

data "aws_availability_zones" "az" {}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.13"
  name    = local.name
  cidr    = "10.40.0.0/16"
  azs             = slice(data.aws_availability_zones.az.names, 0, 3)
  private_subnets = ["10.40.0.0/19", "10.40.32.0/19", "10.40.64.0/19"]
  public_subnets  = ["10.40.96.0/22", "10.40.100.0/22", "10.40.104.0/22"]
  enable_nat_gateway = true
  one_nat_gateway_per_az = var.env == "prod"
  tags = local.tags
}

resource "aws_kms_key" "data" {
  description         = "${local.name} data-at-rest (AES-256)"
  enable_key_rotation = true
  tags                = local.tags
}

module "eks" {
  source          = "terraform-aws-modules/eks/aws"
  version         = "~> 20.24"
  cluster_name    = local.name
  cluster_version = "1.30"
  vpc_id          = module.vpc.vpc_id
  subnet_ids      = module.vpc.private_subnets
  cluster_encryption_config = { resources = ["secrets"], provider_key_arn = aws_kms_key.data.arn }
  eks_managed_node_groups = {
    general = { instance_types = ["m6i.xlarge"], min_size = 3, max_size = 12, desired_size = 3 }
    stream  = { instance_types = ["c6i.2xlarge"], min_size = 2, max_size = 20, desired_size = 3,
                labels = { workload = "stream" } }
  }
  tags = local.tags
}

resource "aws_msk_cluster" "kafka" {
  cluster_name           = local.name
  kafka_version          = "3.6.0"
  number_of_broker_nodes = 3
  broker_node_group_info {
    instance_type   = var.msk_instance_type
    client_subnets  = module.vpc.private_subnets
    security_groups = [aws_security_group.data.id]
    storage_info { ebs_storage_info { volume_size = var.msk_volume_gb } }
  }
  encryption_info {
    encryption_at_rest_kms_key_arn = aws_kms_key.data.arn
    encryption_in_transit { client_broker = "TLS", in_cluster = true }
  }
  configuration_info {
    arn      = aws_msk_configuration.cfg.arn
    revision = aws_msk_configuration.cfg.latest_revision
  }
  tags = local.tags
}

resource "aws_msk_configuration" "cfg" {
  name           = "${local.name}-cfg"
  kafka_versions = ["3.6.0"]
  server_properties = <<-EOT
    default.replication.factor=3
    min.insync.replicas=2
    num.partitions=48
    unclean.leader.election.enable=false
    log.retention.hours=72
  EOT
}

resource "aws_db_subnet_group" "pg" {
  name       = local.name
  subnet_ids = module.vpc.private_subnets
}

resource "aws_db_instance" "pg" {
  identifier                   = local.name
  engine                       = "postgres"
  engine_version               = "16.4"
  instance_class               = var.pg_instance_class
  allocated_storage            = 500
  storage_type                 = "gp3"
  storage_encrypted            = true
  kms_key_id                   = aws_kms_key.data.arn
  multi_az                     = var.env == "prod"
  db_name                      = "fleetpulse"
  username                     = "fleet"
  manage_master_user_password  = true          # password lives in Secrets Manager, rotated
  db_subnet_group_name         = aws_db_subnet_group.pg.name
  vpc_security_group_ids       = [aws_security_group.data.id]
  backup_retention_period      = 14
  deletion_protection          = var.env == "prod"
  performance_insights_enabled = true
  iam_database_authentication_enabled = true
  skip_final_snapshot          = var.env != "prod"
  tags                         = local.tags
}

resource "aws_db_instance" "pg_replica" {
  count               = var.env == "prod" ? 1 : 0
  identifier          = "${local.name}-ro"
  replicate_source_db = aws_db_instance.pg.identifier
  instance_class      = var.pg_instance_class
  storage_encrypted   = true
  tags                = local.tags
}

resource "aws_elasticache_subnet_group" "redis" {
  name       = local.name
  subnet_ids = module.vpc.private_subnets
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = local.name
  description                = "hot state, cache, rate limits"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = 2
  automatic_failover_enabled = true
  multi_az_enabled           = true
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  kms_key_id                 = aws_kms_key.data.arn
  subnet_group_name          = aws_elasticache_subnet_group.redis.name
  security_group_ids         = [aws_security_group.data.id]
  tags                       = local.tags
}

resource "aws_security_group" "data" {
  name   = "${local.name}-data"
  vpc_id = module.vpc.vpc_id
  ingress {
    description     = "from EKS nodes only"
    from_port       = 0
    to_port         = 65535
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Data lake: hot (S3 Standard, 30 d) -> warm (Standard-IA, to 180 d) -> cold (Glacier IR, to 2 y) -> delete
resource "aws_s3_bucket" "lake" {
  bucket = "${local.name}-lake"
  tags   = local.tags
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "lake" {
  bucket                  = aws_s3_bucket.lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id
  rule {
    id     = "raw-telemetry-tiering"
    status = "Enabled"
    filter { prefix = "raw/" }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 180
      storage_class = "GLACIER_IR"
    }
    expiration { days = 730 }
  }
}

provider "helm" {
  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
    }
  }
}

resource "helm_release" "fleetpulse" {
  name      = "fleetpulse"
  chart     = "${path.module}/../../helm/fleetpulse"
  namespace = "fleetpulse"
  create_namespace = true
  values = [yamlencode({
    image  = { tag = var.image_tag }
    config = {
      BUS           = "kafka"
      KAFKA_BROKERS = aws_msk_cluster.kafka.bootstrap_brokers_tls
      REDIS_URL     = "rediss://${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"
      LAKE_DIR      = "s3://${aws_s3_bucket.lake.bucket}/raw"
      PARTITIONS    = "48"
    }
  })]
}
