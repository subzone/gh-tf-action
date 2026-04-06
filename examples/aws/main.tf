terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  required_version = ">= 1.3.0"
}

provider "aws" {
  region = var.region
}

variable "region"      { default = "us-east-1" }
variable "environment" { default = "production" }

# ── Networking ────────────────────────────────────────────────────────────────

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags = { Name = "prod-vpc", Environment = var.environment }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "prod-igw" }
}

resource "aws_subnet" "public_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "${var.region}a"
  map_public_ip_on_launch = true
  tags = { Name = "prod-public-a" }
}

resource "aws_subnet" "private_a" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.10.0/24"
  availability_zone = "${var.region}a"
  tags = { Name = "prod-private-a" }
}

# ── Security groups ───────────────────────────────────────────────────────────
# NOTE: intentionally misconfigured for policy-check demonstration

resource "aws_security_group" "web" {
  name        = "prod-web-sg"
  description = "Web tier"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"] # policy: open ingress
    ipv6_cidr_blocks = ["::/0"]
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  # policy-HIGH: SSH open to the world
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "prod-web-sg" }
}

resource "aws_security_group" "db" {
  name        = "prod-db-sg"
  description = "Database tier"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"] # restricted to VPC
  }
  tags = { Name = "prod-db-sg" }
}

# ── Compute ───────────────────────────────────────────────────────────────────

resource "aws_iam_role" "app" {
  name = "prod-app-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Name = "prod-app-role" }
}

# policy-HIGH: wildcard IAM action and resource
resource "aws_iam_role_policy" "app_s3" {
  name = "prod-app-s3-policy"
  role = aws_iam_role.app.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "*"     # should be scoped to specific actions
      Resource = "*"     # should be scoped to specific resources
    }]
  })
}

# policy-MEDIUM: IMDSv2 not enforced (http_tokens = optional)
resource "aws_launch_template" "app" {
  name          = "prod-app-lt"
  image_id      = "ami-0c55b159cbfafe1f0"
  instance_type = "t3.medium"

  iam_instance_profile {
    arn = aws_iam_role.app.arn
  }

  vpc_security_group_ids = [aws_security_group.web.id]

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "optional" # should be "required"
    http_put_response_hop_limit = 1
  }
  tags = { Name = "prod-app" }
}

resource "aws_autoscaling_group" "app" {
  name               = "prod-app-asg"
  min_size           = 2
  max_size           = 10
  desired_capacity   = 2
  health_check_type  = "ELB"
  vpc_zone_identifier = [aws_subnet.public_a.id]
  target_group_arns  = [aws_lb.main.arn]

  launch_template {
    id      = aws_launch_template.app.id
    version = "$Latest"
  }
  tag { key = "Name"; value = "prod-app"; propagate_at_launch = true }
}

resource "aws_lb" "main" {
  name               = "prod-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.web.id]
  subnets            = [aws_subnet.public_a.id]
  tags = { Name = "prod-alb" }
}

# ── Database ──────────────────────────────────────────────────────────────────

# policy-HIGH: unencrypted storage and publicly accessible
resource "aws_db_instance" "postgres" {
  identifier           = "prod-postgres"
  engine               = "postgres"
  engine_version       = "15.4"
  instance_class       = "db.t3.medium"
  allocated_storage    = 100
  storage_type         = "gp3"
  storage_encrypted    = false           # should be true
  publicly_accessible  = true            # should be false
  skip_final_snapshot  = false
  backup_retention_period = 7
  vpc_security_group_ids  = [aws_security_group.db.id]
  db_subnet_group_name    = aws_subnet.private_a.id
  tags = { Name = "prod-postgres" }
}

resource "aws_elasticache_cluster" "redis" {
  cluster_id           = "prod-redis"
  engine               = "redis"
  engine_version       = "7.0"
  node_type            = "cache.t3.micro"
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  subnet_group_name    = aws_subnet.private_a.id
  security_group_ids   = [aws_security_group.db.id]
  tags = { Name = "prod-redis" }
}

# ── Storage ───────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "assets" {
  bucket = "prod-app-assets-20240101"
  tags   = { Name = "prod-assets", Purpose = "static-assets" }
}

# policy-HIGH: all public access blocks disabled
resource "aws_s3_bucket_public_access_block" "assets" {
  bucket                  = aws_s3_bucket.assets.id
  block_public_acls       = false
  block_public_policy     = false
  restrict_public_buckets = false
  ignore_public_acls      = false
}

resource "aws_s3_bucket" "logs" {
  bucket = "prod-app-logs-20240101"
  tags   = { Name = "prod-logs", Purpose = "access-logs" }
}

# policy-LOW: versioning suspended
resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id
  versioning_configuration { status = "Suspended" }
}

# policy-MEDIUM: no encryption config for aws_s3_bucket.logs
# (no aws_s3_bucket_server_side_encryption_configuration for logs bucket)

# ── Observability ─────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "app" {
  name              = "/prod/app"
  retention_in_days = 30
  tags              = { Name = "prod-app-logs" }
}

# policy-MEDIUM: unencrypted EBS
resource "aws_ebs_volume" "data" {
  availability_zone = "${var.region}a"
  size              = 500
  type              = "gp3"
  encrypted         = false # should be true
  tags              = { Name = "prod-data-volume" }
}

# ── DNS ───────────────────────────────────────────────────────────────────────

resource "aws_route53_zone" "main" {
  name    = "prod.example.com"
  comment = "Managed by Terraform"
  tags    = { Name = "prod-zone" }
}
