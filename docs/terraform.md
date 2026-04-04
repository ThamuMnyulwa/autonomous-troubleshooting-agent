Azure Deployment Shape

- Azure Container Apps service: `intake/api.py`
  Receives Azure DevOps Service Hooks and raw Databricks / Azure Monitor alert webhooks.
- Azure Service Bus queue: `agent-runs`
  Stores one run envelope per investigate/resume action.
- Azure Container Apps Job or queue-driven worker container: `agents/worker.py`
  Pulls one Service Bus message, runs the LangGraph workflow, then exits.
- Azure Key Vault + Managed Identity
  Supplies secrets and Azure auth without baking credentials into images.
- Neon PostgreSQL
  Stores coordination state and LangGraph checkpoints.

Suggested Azure resources

- Container Apps Environment
- Container App for the intake API
- Container Apps Job for the worker
- Service Bus namespace + queue + dead-letter handling
- Key Vault
- User-assigned or system-assigned Managed Identities
- Log Analytics workspace / Application Insights

Secret strategy

- Keep application code on environment variables.
- In Azure, wire those environment variables from Key Vault references on Container Apps and the worker job.
- Prefer Managed Identity for Azure Service Bus and Azure MCP auth.
- Keep `GOOGLE_API_KEY`, `DATABASE_URL`, `ADO_PAT`, `ADO_SERVICE_HOOK_SECRET`, `DATABRICKS_TOKEN`, and `DATABRICKS_MCP_URL` in Key Vault unless they are replaced by stronger identity-based auth.

Queue flow

1. `POST /webhooks/ado` receives a work item event.
2. Intake stores idempotency state in Neon and publishes a run envelope to Service Bus.
3. The worker starts from the queue, processes one message, updates ADO, and settles the queue message.
4. Retry and approval comments enqueue fresh `investigate` or `resume` messages back onto the same queue.

Alert flow

1. Databricks Jobs or Azure Monitor post to `POST /webhooks/alerts`.
2. The intake service normalizes the alert and creates an ADO bug work item.
3. The ADO Service Hook for that bug triggers the standard run-enqueue path.

What stays outside Terraform

- Neon PostgreSQL lifecycle
- Azure DevOps Service Hook subscriptions
- Databricks Job webhook configuration
- MCP server npm packages and container image builds

Implementation note

- The repository runtime code now targets Azure Service Bus and Azure-hosted compute.
- The existing `infra/` Terraform still reflects the older GCP deployment and should be replaced with `azurerm` resources before infrastructure rollout.
