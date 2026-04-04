variable "location" {
  description = "Azure region for Container Apps, Service Bus, and Key Vault resources"
  type        = string
  default     = "westeurope"
}

variable "environment" {
  description = "Deployment environment (dev, staging, prod)"
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

variable "name_prefix" {
  description = "Prefix used to derive Azure resource names"
  type        = string
  default     = "pipeline-resolver"
}

variable "resource_group_name" {
  description = "Optional existing or desired resource group name override"
  type        = string
  default     = ""
}

variable "key_vault_name" {
  description = "Optional Key Vault name override. Must be globally unique and 3-24 alphanumeric chars."
  type        = string
  default     = ""
}

variable "service_bus_namespace_name" {
  description = "Optional Service Bus namespace name override. Must be globally unique."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Additional tags applied to Azure resources"
  type        = map(string)
  default     = {}
}

variable "intake_image" {
  description = "Container image for the FastAPI intake service"
  type        = string
}

variable "worker_image" {
  description = "Container image for the worker job"
  type        = string
}

variable "container_registry_server" {
  description = "Optional container registry server, for example myregistry.azurecr.io"
  type        = string
  default     = ""
}

variable "container_registry_resource_id" {
  description = "Optional container registry resource ID used for AcrPull role assignments"
  type        = string
  default     = ""
}

variable "google_api_key" {
  description = "Gemini API key"
  type        = string
  sensitive   = true
}

variable "database_url" {
  description = "Neon PostgreSQL connection string (must include sslmode=require)"
  type        = string
  sensitive   = true
}

variable "ado_pat" {
  description = "Azure DevOps PAT (Work Items read/write, Code read)"
  type        = string
  sensitive   = true
}

variable "ado_service_hook_secret" {
  description = "Shared secret for ADO Service Hook HMAC validation"
  type        = string
  sensitive   = true
}

variable "ado_org_url" {
  description = "ADO organization URL, e.g. https://dev.azure.com/your-org"
  type        = string
}

variable "ado_project" {
  description = "ADO project name"
  type        = string
}

variable "ado_area_path" {
  description = "Optional ADO area path filter for agent work items"
  type        = string
  default     = ""
}

variable "databricks_host" {
  description = "Databricks workspace URL, e.g. https://adb-xxx.azuredatabricks.net"
  type        = string
}

variable "databricks_token" {
  description = "Databricks service principal PAT or M2M OAuth token"
  type        = string
  sensitive   = true
}

variable "databricks_warehouse_id" {
  description = "Databricks SQL warehouse ID for system table queries"
  type        = string
  default     = ""
}

variable "databricks_mcp_url" {
  description = "Explicit Databricks MCP endpoint for the worker"
  type        = string
}

variable "azure_tenant_id" {
  description = "Optional tenant override for Azure auth. Defaults to the current Terraform tenant."
  type        = string
  default     = ""
}

variable "azure_client_id" {
  description = "Optional Azure client ID override for the worker MCP path"
  type        = string
  default     = ""
}

variable "azure_client_secret" {
  description = "Optional Azure client secret for the worker MCP path"
  type        = string
  sensitive   = true
  default     = ""
}

variable "azure_service_bus_queue_name" {
  description = "Queue name for investigation and resume run envelopes"
  type        = string
  default     = "agent-runs"
}

variable "azure_service_bus_receive_wait_seconds" {
  description = "Seconds the worker waits for a queue message before exiting"
  type        = number
  default     = 5
}

variable "azure_service_bus_lock_renewal_seconds" {
  description = "Maximum lock renewal duration for a single worker execution"
  type        = number
  default     = 3600
}

variable "service_bus_sku" {
  description = "Service Bus namespace SKU"
  type        = string
  default     = "Standard"
}

variable "service_bus_local_auth_enabled" {
  description = "Enable SAS/local auth on the Service Bus namespace"
  type        = bool
  default     = false
}

variable "service_bus_message_ttl" {
  description = "Default TTL for Service Bus queue messages in ISO-8601 duration format"
  type        = string
  default     = "P7D"
}

variable "service_bus_duplicate_detection_window" {
  description = "Duplicate detection window for the Service Bus queue"
  type        = string
  default     = "PT10M"
}

variable "service_bus_lock_duration" {
  description = "Peek-lock duration for queue messages"
  type        = string
  default     = "PT5M"
}

variable "service_bus_max_delivery_count" {
  description = "Max delivery attempts before dead-lettering a queue message"
  type        = number
  default     = 5
}

variable "intake_port" {
  description = "HTTP port exposed by the FastAPI intake service"
  type        = number
  default     = 8080
}

variable "log_level" {
  description = "Application log level"
  type        = string
  default     = "INFO"
}

variable "intake_cpu" {
  description = "vCPU allocation for the intake container"
  type        = number
  default     = 0.5
}

variable "intake_memory" {
  description = "Memory allocation for the intake container"
  type        = string
  default     = "1Gi"
}

variable "intake_min_replicas" {
  description = "Minimum replica count for the intake Container App"
  type        = number
  default     = 1
}

variable "intake_max_replicas" {
  description = "Maximum replica count for the intake Container App"
  type        = number
  default     = 3
}

variable "worker_cpu" {
  description = "vCPU allocation for the worker job container"
  type        = number
  default     = 1
}

variable "worker_memory" {
  description = "Memory allocation for the worker job container"
  type        = string
  default     = "2Gi"
}

variable "worker_replica_timeout_in_seconds" {
  description = "Timeout for a single worker job replica"
  type        = number
  default     = 3600
}

variable "worker_replica_retry_limit" {
  description = "Retry limit for failed worker job replicas"
  type        = number
  default     = 0
}

variable "worker_schedule_cron_expression" {
  description = "Cron expression for the worker polling job"
  type        = string
  default     = "*/1 * * * *"
}

variable "log_analytics_retention_days" {
  description = "Retention period for Log Analytics workspace data"
  type        = number
  default     = 30
}

variable "key_vault_purge_protection_enabled" {
  description = "Enable purge protection on Key Vault"
  type        = bool
  default     = false
}

variable "key_vault_soft_delete_retention_days" {
  description = "Soft delete retention period for Key Vault"
  type        = number
  default     = 7
}

variable "max_iterations" {
  description = "Hard stop on ReAct loop iterations"
  type        = number
  default     = 5
}

variable "max_attempts" {
  description = "Max HITL retry attempts before escalation"
  type        = number
  default     = 3
}

variable "checkpoint_ttl_days" {
  description = "Days to retain LangGraph checkpoints before cleanup"
  type        = number
  default     = 7
}
