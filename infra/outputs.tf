output "resource_group_name" {
  description = "Resource group containing the Azure deployment"
  value       = azurerm_resource_group.main.name
}

output "intake_url" {
  description = "Public base URL for the FastAPI intake service"
  value       = "https://${azurerm_container_app.intake.latest_revision_fqdn}"
}

output "ado_webhook_url" {
  description = "Azure DevOps Service Hook target URL"
  value       = "https://${azurerm_container_app.intake.latest_revision_fqdn}/webhooks/ado"
}

output "alerts_webhook_url" {
  description = "Databricks and Azure Monitor alert target URL"
  value       = "https://${azurerm_container_app.intake.latest_revision_fqdn}/webhooks/alerts"
}

output "service_bus_namespace_name" {
  description = "Azure Service Bus namespace name"
  value       = azurerm_servicebus_namespace.main.name
}

output "service_bus_queue_name" {
  description = "Azure Service Bus queue name for worker envelopes"
  value       = azurerm_servicebus_queue.agent_runs.name
}

output "key_vault_name" {
  description = "Key Vault name used for application secrets"
  value       = azurerm_key_vault.main.name
}

output "worker_job_name" {
  description = "Azure Container Apps Job name for the queue-polling worker"
  value       = azurerm_container_app_job.worker.name
}

output "intake_identity_client_id" {
  description = "Client ID of the intake managed identity"
  value       = azurerm_user_assigned_identity.intake.client_id
}

output "worker_identity_client_id" {
  description = "Client ID of the worker managed identity"
  value       = azurerm_user_assigned_identity.worker.client_id
}
