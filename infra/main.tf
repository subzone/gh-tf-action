terraform {
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
  }
  required_version = ">= 1.3.0"
}

# ── Resources ──────────────────────────────────────────────────────────────────

resource "null_resource" "web_server" {
  triggers = {
    id = "web-server-1"
  }
}

resource "null_resource" "database" {
  triggers = {
    id = "db-primary"
  }
  depends_on = [null_resource.web_server]
}

resource "null_resource" "cache" {
  triggers = {
    id = "redis-cache"
  }
  depends_on = [null_resource.web_server]
}

resource "null_resource" "worker" {
  triggers = {
    id = "background-worker"
  }
  depends_on = [null_resource.database, null_resource.cache]
}

resource "null_resource" "load_balancer" {
  triggers = {
    id = "lb-frontend"
  }
  depends_on = [null_resource.web_server]
}
