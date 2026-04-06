terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
  required_version = ">= 1.3.0"
}

provider "azurerm" {
  features {}
}

variable "location"        { default = "West Europe" }
variable "environment"     { default = "production" }
variable "resource_prefix" { default = "prod" }

# ── Resource Group ────────────────────────────────────────────────────────────

resource "azurerm_resource_group" "main" {
  name     = "${var.resource_prefix}-rg"
  location = var.location
  tags     = { Environment = var.environment, ManagedBy = "Terraform" }
}

# ── Networking ────────────────────────────────────────────────────────────────

resource "azurerm_virtual_network" "main" {
  name                = "${var.resource_prefix}-vnet"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  address_space       = ["10.0.0.0/16"]
  tags                = { Environment = var.environment }
}

resource "azurerm_subnet" "web" {
  name                 = "${var.resource_prefix}-web-subnet"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.0.1.0/24"]
}

resource "azurerm_subnet" "data" {
  name                 = "${var.resource_prefix}-data-subnet"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.0.10.0/24"]
}

resource "azurerm_network_security_group" "web" {
  name                = "${var.resource_prefix}-web-nsg"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  security_rule {
    name                       = "Allow-HTTP"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "Allow-HTTPS"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
  tags = { Environment = var.environment }
}

# ── Storage ───────────────────────────────────────────────────────────────────
# NOTE: intentionally misconfigured for policy-check demonstration

# policy-HIGH: public blob access and HTTP traffic allowed
resource "azurerm_storage_account" "assets" {
  name                     = "${var.resource_prefix}assets20240101"
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = "LRS"

  allow_blob_public_access  = true  # policy-HIGH
  enable_https_traffic_only = false # policy-HIGH
  min_tls_version           = "TLS1_0"

  tags = { Purpose = "static-assets" }
}

resource "azurerm_storage_account" "logs" {
  name                     = "${var.resource_prefix}logs20240101"
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = "GRS"

  allow_blob_public_access  = false
  enable_https_traffic_only = true # compliant
  min_tls_version           = "TLS1_2"

  tags = { Purpose = "audit-logs" }
}

# ── App Service ───────────────────────────────────────────────────────────────

resource "azurerm_service_plan" "main" {
  name                = "${var.resource_prefix}-asp"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  os_type             = "Linux"
  sku_name            = "S2"
  tags                = { Environment = var.environment }
}

resource "azurerm_linux_web_app" "api" {
  name                = "${var.resource_prefix}-api"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  service_plan_id     = azurerm_service_plan.main.id
  https_only          = false # should be true

  site_config {
    application_stack { node_version = "18-lts" }
    always_on = true
  }

  app_settings = {
    APPINSIGHTS_INSTRUMENTATIONKEY = azurerm_application_insights.main.instrumentation_key
  }
  tags = { Environment = var.environment }
}

# ── Database ──────────────────────────────────────────────────────────────────

resource "azurerm_postgresql_flexible_server" "main" {
  name                = "${var.resource_prefix}-postgres"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  administrator_login    = "psqladmin"
  administrator_password = "ChangeMe123!" # use key vault reference in production
  version                = "15"
  sku_name               = "GP_Standard_D4s_v3"
  storage_mb             = 51200

  backup_retention_days        = 7
  geo_redundant_backup_enabled = false

  delegated_subnet_id = azurerm_subnet.data.id
  tags = { Environment = var.environment }
}

resource "azurerm_redis_cache" "main" {
  name                = "${var.resource_prefix}-redis"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  capacity            = 1
  family              = "C"
  sku_name            = "Standard"
  enable_non_ssl_port = false
  minimum_tls_version = "1.2"
  subnet_id           = azurerm_subnet.data.id
  tags = { Environment = var.environment }
}

# ── Security ──────────────────────────────────────────────────────────────────

resource "azurerm_key_vault" "main" {
  name                        = "${var.resource_prefix}-kv-20240101"
  location                    = azurerm_resource_group.main.location
  resource_group_name         = azurerm_resource_group.main.name
  tenant_id                   = data.azurerm_client_config.current.tenant_id
  sku_name                    = "standard"
  soft_delete_retention_days  = 90
  purge_protection_enabled    = true
  enabled_for_disk_encryption = true
  tags = { Environment = var.environment }
}

data "azurerm_client_config" "current" {}

# ── Observability ─────────────────────────────────────────────────────────────

resource "azurerm_log_analytics_workspace" "main" {
  name                = "${var.resource_prefix}-law"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags = { Environment = var.environment }
}

resource "azurerm_application_insights" "main" {
  name                = "${var.resource_prefix}-appinsights"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "web"
  retention_in_days   = 90
  tags = { Environment = var.environment }
}

# ── CDN ───────────────────────────────────────────────────────────────────────

resource "azurerm_cdn_profile" "main" {
  name                = "${var.resource_prefix}-cdn"
  location            = "global"
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "Standard_Microsoft"
  tags = { Environment = var.environment }
}

resource "azurerm_cdn_endpoint" "assets" {
  name                = "${var.resource_prefix}-cdn-assets"
  profile_name        = azurerm_cdn_profile.main.name
  location            = "global"
  resource_group_name = azurerm_resource_group.main.name
  is_https_allowed    = true
  is_http_allowed     = true

  origin {
    name      = "storage"
    host_name = azurerm_storage_account.assets.primary_blob_host
  }
  tags = { Environment = var.environment }
}
