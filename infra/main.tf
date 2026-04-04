data "azurerm_client_config" "current" {}

resource "random_string" "suffix" {
  length  = 5
  lower   = true
  upper   = false
  numeric = true
  special = false
}

locals {
  base_name = replace(lower("${var.name_prefix}-${var.environment}"), "/[^a-z0-9-]/", "-")

  resource_group_name = var.resource_group_name != "" ? var.resource_group_name : "${local.base_name}-rg"

  key_vault_name = var.key_vault_name != "" ? var.key_vault_name : substr(
    replace(lower("${var.name_prefix}${var.environment}${random_string.suffix.result}"), "/[^a-z0-9]/", ""),
    0,
    24,
  )

  service_bus_namespace_name = var.service_bus_namespace_name != "" ? var.service_bus_namespace_name : substr(
    replace(lower("${var.name_prefix}-${var.environment}-${random_string.suffix.result}-sb"), "/[^a-z0-9-]/", "-"),
    0,
    50,
  )

  tenant_id = var.azure_tenant_id != "" ? var.azure_tenant_id : data.azurerm_client_config.current.tenant_id

  worker_azure_client_id = var.azure_client_id != "" ? var.azure_client_id : azurerm_user_assigned_identity.worker.client_id

  tags = merge(
    {
      environment = var.environment
      managed-by  = "terraform"
      service     = "pipeline-resolver"
    },
    var.tags,
  )

  key_vault_secret_values = merge(
    {
      "ado-pat"                 = var.ado_pat
      "ado-service-hook-secret" = var.ado_service_hook_secret
      "database-url"            = var.database_url
      "databricks-token"        = var.databricks_token
      "google-api-key"          = var.google_api_key
    },
    var.azure_client_secret != "" ? { "azure-client-secret" = var.azure_client_secret } : {},
  )

  intake_secret_names = toset([
    "ado-pat",
    "ado-service-hook-secret",
    "database-url",
  ])

  worker_secret_names = toset(
    concat(
      [
        "ado-pat",
        "database-url",
        "databricks-token",
        "google-api-key",
      ],
      var.azure_client_secret != "" ? ["azure-client-secret"] : [],
    )
  )
}

resource "azurerm_resource_group" "main" {
  name     = local.resource_group_name
  location = var.location
  tags     = local.tags
}

resource "azurerm_log_analytics_workspace" "main" {
  name                = substr("${local.base_name}-law", 0, 63)
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  retention_in_days   = var.log_analytics_retention_days
  sku                 = "PerGB2018"
  tags                = local.tags
}

resource "azurerm_container_app_environment" "main" {
  name                       = substr("${local.base_name}-cae", 0, 32)
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  tags                       = local.tags
}

resource "azurerm_user_assigned_identity" "intake" {
  name                = substr("${local.base_name}-intake-mi", 0, 64)
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_user_assigned_identity" "worker" {
  name                = substr("${local.base_name}-worker-mi", 0, 64)
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_servicebus_namespace" "main" {
  name                          = local.service_bus_namespace_name
  location                      = azurerm_resource_group.main.location
  resource_group_name           = azurerm_resource_group.main.name
  sku                           = var.service_bus_sku
  local_auth_enabled            = var.service_bus_local_auth_enabled
  public_network_access_enabled = true
  minimum_tls_version           = "1.2"
  tags                          = local.tags
}

resource "azurerm_servicebus_queue" "agent_runs" {
  name                                    = var.azure_service_bus_queue_name
  namespace_id                            = azurerm_servicebus_namespace.main.id
  dead_lettering_on_message_expiration    = true
  default_message_ttl                     = var.service_bus_message_ttl
  duplicate_detection_history_time_window = var.service_bus_duplicate_detection_window
  lock_duration                           = var.service_bus_lock_duration
  max_delivery_count                      = var.service_bus_max_delivery_count
  partitioning_enabled                    = false
  requires_duplicate_detection            = true
}

resource "azurerm_key_vault" "main" {
  name                          = local.key_vault_name
  location                      = azurerm_resource_group.main.location
  resource_group_name           = azurerm_resource_group.main.name
  tenant_id                     = local.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = var.key_vault_purge_protection_enabled
  public_network_access_enabled = true
  soft_delete_retention_days    = var.key_vault_soft_delete_retention_days
  tags                          = local.tags
}

resource "azurerm_key_vault_secret" "app" {
  for_each     = local.key_vault_secret_values
  key_vault_id = azurerm_key_vault.main.id
  name         = each.key
  value        = each.value
  tags         = local.tags
}

resource "azurerm_role_assignment" "intake_key_vault" {
  scope                            = azurerm_key_vault.main.id
  role_definition_name             = "Key Vault Secrets User"
  principal_id                     = azurerm_user_assigned_identity.intake.principal_id
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "worker_key_vault" {
  scope                            = azurerm_key_vault.main.id
  role_definition_name             = "Key Vault Secrets User"
  principal_id                     = azurerm_user_assigned_identity.worker.principal_id
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "intake_service_bus_sender" {
  scope                            = azurerm_servicebus_namespace.main.id
  role_definition_name             = "Azure Service Bus Data Sender"
  principal_id                     = azurerm_user_assigned_identity.intake.principal_id
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "worker_service_bus_receiver" {
  scope                            = azurerm_servicebus_namespace.main.id
  role_definition_name             = "Azure Service Bus Data Receiver"
  principal_id                     = azurerm_user_assigned_identity.worker.principal_id
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "intake_acr_pull" {
  count = var.container_registry_resource_id != "" ? 1 : 0

  scope                            = var.container_registry_resource_id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_user_assigned_identity.intake.principal_id
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "worker_acr_pull" {
  count = var.container_registry_resource_id != "" ? 1 : 0

  scope                            = var.container_registry_resource_id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_user_assigned_identity.worker.principal_id
  skip_service_principal_aad_check = true
}

resource "azurerm_container_app" "intake" {
  name                         = substr("${local.base_name}-intake", 0, 32)
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.intake.id]
  }

  dynamic "registry" {
    for_each = var.container_registry_server != "" ? [1] : []
    content {
      server   = var.container_registry_server
      identity = azurerm_user_assigned_identity.intake.id
    }
  }

  dynamic "secret" {
    for_each = local.intake_secret_names
    content {
      name                = secret.value
      key_vault_secret_id = azurerm_key_vault_secret.app[secret.value].versionless_id
      identity            = azurerm_user_assigned_identity.intake.id
    }
  }

  ingress {
    allow_insecure_connections = false
    external_enabled           = true
    target_port                = var.intake_port
    transport                  = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = var.intake_min_replicas
    max_replicas = var.intake_max_replicas

    container {
      name   = "intake"
      image  = var.intake_image
      cpu    = var.intake_cpu
      memory = var.intake_memory

      env {
        name        = "ADO_PAT"
        secret_name = "ado-pat"
      }
      env {
        name        = "ADO_SERVICE_HOOK_SECRET"
        secret_name = "ado-service-hook-secret"
      }
      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }
      env {
        name  = "ADO_AREA_PATH"
        value = var.ado_area_path
      }
      env {
        name  = "ADO_ORG_URL"
        value = var.ado_org_url
      }
      env {
        name  = "ADO_PROJECT"
        value = var.ado_project
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.intake.client_id
      }
      env {
        name  = "AZURE_SERVICE_BUS_NAMESPACE"
        value = "${azurerm_servicebus_namespace.main.name}.servicebus.windows.net"
      }
      env {
        name  = "AZURE_SERVICE_BUS_QUEUE_NAME"
        value = azurerm_servicebus_queue.agent_runs.name
      }
      env {
        name  = "DATABRICKS_HOST"
        value = var.databricks_host
      }
      env {
        name  = "INTAKE_PORT"
        value = tostring(var.intake_port)
      }
      env {
        name  = "LOG_LEVEL"
        value = var.log_level
      }

      liveness_probe {
        path      = "/healthz"
        port      = var.intake_port
        transport = "HTTP"
      }

      readiness_probe {
        path      = "/healthz"
        port      = var.intake_port
        transport = "HTTP"
      }
    }
  }

  depends_on = [
    azurerm_role_assignment.intake_key_vault,
    azurerm_role_assignment.intake_service_bus_sender,
    azurerm_role_assignment.intake_acr_pull,
  ]
}

resource "azurerm_container_app_job" "worker" {
  name                         = substr("${local.base_name}-worker", 0, 32)
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  replica_retry_limit          = var.worker_replica_retry_limit
  replica_timeout_in_seconds   = var.worker_replica_timeout_in_seconds
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.worker.id]
  }

  dynamic "registry" {
    for_each = var.container_registry_server != "" ? [1] : []
    content {
      server   = var.container_registry_server
      identity = azurerm_user_assigned_identity.worker.id
    }
  }

  dynamic "secret" {
    for_each = local.worker_secret_names
    content {
      name                = secret.value
      key_vault_secret_id = azurerm_key_vault_secret.app[secret.value].versionless_id
      identity            = azurerm_user_assigned_identity.worker.id
    }
  }

  schedule_trigger_config {
    cron_expression          = var.worker_schedule_cron_expression
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "worker"
      image  = var.worker_image
      cpu    = var.worker_cpu
      memory = var.worker_memory

      env {
        name        = "ADO_PAT"
        secret_name = "ado-pat"
      }
      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }
      env {
        name        = "DATABRICKS_TOKEN"
        secret_name = "databricks-token"
      }
      env {
        name        = "GOOGLE_API_KEY"
        secret_name = "google-api-key"
      }
      env {
        name  = "ADO_AREA_PATH"
        value = var.ado_area_path
      }
      env {
        name  = "ADO_ORG_URL"
        value = var.ado_org_url
      }
      env {
        name  = "ADO_PROJECT"
        value = var.ado_project
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = local.worker_azure_client_id
      }
      env {
        name  = "AZURE_SERVICE_BUS_LOCK_RENEWAL_SECONDS"
        value = tostring(var.azure_service_bus_lock_renewal_seconds)
      }
      env {
        name  = "AZURE_SERVICE_BUS_NAMESPACE"
        value = "${azurerm_servicebus_namespace.main.name}.servicebus.windows.net"
      }
      env {
        name  = "AZURE_SERVICE_BUS_QUEUE_NAME"
        value = azurerm_servicebus_queue.agent_runs.name
      }
      env {
        name  = "AZURE_SERVICE_BUS_RECEIVE_WAIT_SECONDS"
        value = tostring(var.azure_service_bus_receive_wait_seconds)
      }
      env {
        name  = "AZURE_TENANT_ID"
        value = local.tenant_id
      }
      env {
        name  = "CHECKPOINT_TTL_DAYS"
        value = tostring(var.checkpoint_ttl_days)
      }
      env {
        name  = "DATABRICKS_HOST"
        value = var.databricks_host
      }
      env {
        name  = "DATABRICKS_MCP_URL"
        value = var.databricks_mcp_url
      }
      env {
        name  = "DATABRICKS_WAREHOUSE_ID"
        value = var.databricks_warehouse_id
      }
      env {
        name  = "LOG_LEVEL"
        value = var.log_level
      }
      env {
        name  = "MAX_ATTEMPTS"
        value = tostring(var.max_attempts)
      }
      env {
        name  = "MAX_ITERATIONS"
        value = tostring(var.max_iterations)
      }

      dynamic "env" {
        for_each = var.azure_client_secret != "" ? [1] : []
        content {
          name        = "AZURE_CLIENT_SECRET"
          secret_name = "azure-client-secret"
        }
      }
    }
  }

  depends_on = [
    azurerm_role_assignment.worker_key_vault,
    azurerm_role_assignment.worker_service_bus_receiver,
    azurerm_role_assignment.worker_acr_pull,
  ]
}
