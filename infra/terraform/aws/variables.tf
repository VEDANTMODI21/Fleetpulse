variable "region" {
  type    = string
  default = "ap-south-1" # Mumbai: keeps Indian vehicle data in-region (DPDP)
}
variable "env" {
  type    = string
  default = "staging"
}
variable "image_tag" {
  type    = string
  default = "1.0.0"
}
variable "msk_instance_type" {
  type    = string
  default = "kafka.m7g.large"
}
variable "msk_volume_gb" {
  type    = number
  default = 2000
}
variable "pg_instance_class" {
  type    = string
  default = "db.r6g.xlarge"
}
variable "redis_node_type" {
  type    = string
  default = "cache.r7g.large"
}
