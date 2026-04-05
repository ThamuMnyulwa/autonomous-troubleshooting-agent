# Azure DevOps Setup Guide

This guide walks through setting up Azure DevOps as the coordination layer for the pipeline-resolver agent. ADO provides the work item tracking, Service Hooks (webhooks), and human-in-the-loop comment flow that the agent relies on.

## Prerequisites

- An Azure account with an active subscription (the agent infra runs here)
- Azure CLI installed (`az --version`)
- Azure DevOps CLI extension (`az extension add --name azure-devops`)

## 1. Create an Azure DevOps Organisation

ADO orgs are created through the portal — there is no CLI command for this.

1. Go to https://dev.azure.com
2. Sign in with your Azure AD account (e.g. `user@yourorg.onmicrosoft.com`)
3. Click **"Create new organization"**
4. Choose a name (e.g. `myorg`) and a region close to your Azure resources

Your org URL will be: `https://dev.azure.com/{org-name}`

> **Cost:** Azure DevOps is free for up to 5 Basic users. Boards, work items, and Service Hooks are all included at no cost.

## 2. Authenticate the Azure DevOps CLI

The `az devops` extension needs its own authentication — a standard `az login` token does not work for ADO API calls. You have two options:

### Option A: Personal Access Token (recommended for CLI scripting)

1. Go to `https://dev.azure.com/{org}/_usersSettings/tokens`
2. Click **"New Token"**
3. Configure:
   - **Name:** `pipeline-resolver-cli`
   - **Organization:** your org
   - **Expiration:** 90 days (or custom)
   - **Scopes:** Full access (scope down later per the table below)
4. Copy the token immediately (you won't see it again)
5. Export it in your shell session:

```bash
export AZURE_DEVOPS_EXT_PAT=<your-token>
```

The `az devops` extension automatically picks up `AZURE_DEVOPS_EXT_PAT` for all commands.

### Option B: Azure AD bearer token

```bash
az login --scope 499b84ac-1321-427f-aa17-267ca6975798/.default
```

This requests a token scoped to the Azure DevOps resource ID. It works but can be flaky — the PAT approach is more reliable for automation.

### Set CLI defaults

```bash
az devops configure --defaults \
  organization=https://dev.azure.com/{org} \
  project={project-name}
```

## 3. Create the Project

```bash
az devops project create \
  --name "dassie" \
  --description "Autonomous pipeline troubleshooting agent" \
  --source-control git \
  --process "Basic" \
  --visibility private \
  --org https://dev.azure.com/{org}
```

The **Basic** process template provides Issue, Task, and Epic work item types. The agent uses **Issue** for pipeline failure incidents by default. If your project uses a different workflow, set `ADO_WORK_ITEM_TYPE` accordingly and, for custom state names, override the lifecycle mapping with `ADO_STATE_QUEUED`, `ADO_STATE_INVESTIGATING`, `ADO_STATE_AWAITING_APPROVAL`, `ADO_STATE_RESOLVED`, and `ADO_STATE_ESCALATED`.

Verify:

```bash
az devops project list --org https://dev.azure.com/{org} -o table
```

## 4. Create Area Paths

Area paths scope which work items the agent processes. The intake API filters on area path to avoid picking up unrelated bugs.

```bash
az boards area project create \
  --name "pipeline-resolver" \
  --org https://dev.azure.com/{org} \
  -p dassie
```

This creates the area path `dassie\pipeline-resolver`. Set this as `ADO_AREA_PATH` in your environment / Terraform variables.

## 5. Create a PAT for the Agent Runtime

The agent itself needs a PAT to interact with ADO at runtime. Create a separate, scoped token:

1. Go to `https://dev.azure.com/{org}/_usersSettings/tokens`
2. Click **"New Token"**
3. Configure:
   - **Name:** `pipeline-resolver-agent`
   - **Organization:** your org
   - **Expiration:** 365 days (maximum)
   - **Scopes:** Custom defined:

| Scope | Access | Why |
|-------|--------|-----|
| Work Items | Read, Write, & Manage | Create bugs, update state, post comments |
| Code | Read | Read repo contents for fix proposals |
| Service Hooks | Read & Write | (Optional) Programmatic subscription management |

4. Store the token:
   - In Key Vault: `ado-pat`
   - In `infra/dev.tfvars`: `ado_pat = "<token>"`
   - Or as env var: `TF_VAR_ado_pat=<token>`

## 6. Generate a Webhook Secret

The intake API validates ADO Service Hook payloads using HMAC-SHA256. Generate a shared secret:

```bash
# Generate a random 32-byte hex secret
openssl rand -hex 32
```

Store it:
- In Key Vault: `ado-service-hook-secret`
- In `infra/dev.tfvars`: `ado_service_hook_secret = "<secret>"`
- Or as env var: `TF_VAR_ado_service_hook_secret=<secret>`

## 7. Configure Service Hooks

Service Hooks send real-time HTTP POSTs to the intake API when work item events occur. You need two subscriptions:

### workitem.created — triggers new investigations

After the intake Container App is deployed and has a public URL:

```bash
# Get the intake URL from Terraform output
INTAKE_URL=$(cd infra && terraform output -raw intake_url)

az devops service-endpoint create ...  # or use the portal
```

**Portal method** (more reliable for initial setup):

1. Go to `https://dev.azure.com/{org}/{project}/_settings/serviceHooks`
2. Click **"+"** → **Web Hooks**
3. Configure:
   - **Trigger:** Work item created
   - **Filters:**
     - Area path: `dassie\pipeline-resolver`
     - Work item type: Bug
     - Tag: `agent:run`
   - **Action:**
     - URL: `https://<intake-fqdn>/webhooks/ado`
     - HTTP headers: (none needed — HMAC validates the body)
     - Resource details to send: All
     - Messages to send: All

### workitem.commented — handles approval and retry

1. Same flow as above but:
   - **Trigger:** Work item commented
   - **Filters:**
     - Area path: `dassie\pipeline-resolver`
   - **Action:** Same URL: `https://<intake-fqdn>/webhooks/ado`

The intake API (`intake/api.py`) parses comment text for:
- **`approve`** → resumes the agent to execute a pending write action
- **`did not work`** → triggers a retry investigation with the new error context

## 8. Create Seed Work Items

Create a test Issue to verify the end-to-end flow:

```bash
az boards work-item create \
  --type Issue \
  --title "Test: pipeline-resolver agent smoke test" \
  --area "dassie\\pipeline-resolver" \
  --fields "System.Tags=agent:run" \
  --description "<!-- agent-metadata
environment: databricks
failure_type: test
-->
This is a smoke test to verify the agent picks up work items correctly." \
  --org https://dev.azure.com/{org} \
  -p dassie
```

The `agent:run` tag is what tells the intake API to enqueue this bug for investigation.

### Work item description format

The agent expects an optional `<!-- agent-metadata -->` HTML comment block in the description with key-value pairs:

```html
<!-- agent-metadata
environment: databricks
failure_type: job_failure
job_id: 12345
cluster_id: 0404-123456-abcde
-->
<p>Human-readable incident description here...</p>
```

The `glue/normaliser.py` module automatically formats this block when creating bugs from Databricks or Azure Monitor alert webhooks.

## 9. Update Terraform Variables

Update `infra/dev.tfvars` with your ADO details:

```hcl
ado_org_url             = "https://dev.azure.com/{org}"
ado_project             = "dassie"
ado_area_path           = "dassie\\pipeline-resolver"
ado_pat                 = "<agent-runtime-pat>"
ado_service_hook_secret = "<generated-secret>"
```

These get stored in Key Vault and injected into the Container Apps as environment variables.

## 10. Verify the Integration

Once the infrastructure is deployed:

```bash
# 1. Check the intake API is healthy
curl https://<intake-fqdn>/healthz

# 2. Create a test bug (should trigger the Service Hook)
az boards work-item create \
  --type Bug \
  --title "E2E test: verify agent pickup" \
  --area "dassie\\pipeline-resolver" \
  --fields "System.Tags=agent:run" \
  --description "Test incident" \
  --org https://dev.azure.com/{org} -p dassie

# 3. Check the intake API logs for the webhook receipt
az containerapp logs show --name pipeline-resolver-dev-intake \
  --resource-group pipeline-resolver-dev-rg \
  --follow

# 4. Check the worker job logs
az containerapp job execution list --name pipeline-resolver-dev-worker \
  --resource-group pipeline-resolver-dev-rg -o table
```

## Architecture Reference

```
Alert Sources → POST /webhooks/alerts → normaliser → ADO Bug (tagged agent:run)
                                                          ↓
                              ADO Service Hook (workitem.created)
                                                          ↓
                              POST /webhooks/ado → intake API
                                                          ↓
                                    Neon DB (idempotency) + Service Bus queue
                                                          ↓
                                    Worker (LangGraph ReAct loop + MCP tools)
                                                          ↓
                                    ADO comment (diagnosis / approval request)
                                                          ↓
                              Human replies "approve" or "did not work"
                                                          ↓
                              ADO Service Hook (workitem.commented)
                                                          ↓
                              intake API → resume or retry via Service Bus
```

## PAT Scope Reference

| Component | Required Scopes | Notes |
|-----------|----------------|-------|
| CLI setup (`az devops`) | Full access or Work Items + Project | One-time setup use |
| Agent runtime (`ado_pat`) | Work Items R/W, Code Read | Long-lived, stored in Key Vault |
| Service Hook validation | N/A (uses shared secret, not PAT) | HMAC-SHA256 via `ado_service_hook_secret` |

## Troubleshooting

**`TF400813: The user is not authorized`**
- The `az` CLI bearer token doesn't work for ADO. Set `AZURE_DEVOPS_EXT_PAT` or re-login with `az login --scope 499b84ac-1321-427f-aa17-267ca6975798/.default`.

**`Bug does not exist in project`** / **`Issue does not exist in project`**
- Your project's process template determines the available types. Basic uses `Issue`, Agile uses `Bug`, Scrum uses `Product Backlog Item`. Set `ADO_WORK_ITEM_TYPE` to match your template. Check available types with: `az rest --method get --url "https://dev.azure.com/{org}/{project}/_apis/wit/workitemtypes?api-version=7.1" --resource "499b84ac-1321-427f-aa17-267ca6975798" --query 'value[].name'`

**Service Hook not firing**
- Verify the target URL is publicly reachable over HTTPS.
- Check that the work item matches all filters (area path, type, tags).
- ADO auto-disables hooks after repeated failures — check the subscription status in Project Settings → Service Hooks.

**HMAC validation failing on intake**
- Ensure the `ado_service_hook_secret` in Key Vault matches exactly what you configured in the Service Hook subscription.
- The intake API expects the signature in the `X-Hub-Signature` header.


---

# Some thoughts for future consideration

 Use a dedicated incident board, not your normal engineering backlog.

  Recommended Setup

  - Create a separate Azure DevOps team and board for the agent, for example Pipeline Resolver.
  - Scope it to a dedicated area path like Project\\pipeline-resolver, which already matches your repo docs in docs/ado-setup.md:82.
  - Use one work item type for incidents only.
    If you want minimum change with the current code, keep Bug or Issue via ADO_WORK_ITEM_TYPE as described in docs/ado-setup.md:74.
    If you want the cleanest long-term board, create a custom WIT like Agent Incident in an inherited process and point ADO_WORK_ITEM_TYPE at it.

  Board Shape
  I would make the board look like this:

  - New
  - Investigating
  - Awaiting Approval
  - Closed

  And I would map your internal run states from db.py:32 like this:

  - QUEUED -> New
  - INVESTIGATING -> Investigating
  - AWAITING_APPROVAL -> Awaiting Approval
  - RESOLVED / ESCALATED -> Closed

  Important detail: Azure Boards only allows one state in the Completed category, so don’t model both Resolved and Escalated as separate final workflow states. Use a custom field like Agent Outcome with
  values:

  - Resolved
  - Escalated
  - False Positive

  That gives you one clean terminal column and still preserves operational outcome.

  Fields I’d Add
  Add these as first-class custom fields instead of hiding them only in HTML metadata:

  - Target Environment
  - Failure Type
  - Alert Source
  - Attempt Count
  - Agent Outcome
  - Pending Action
  - Workspace URL
  - Job ID
  - Run ID

  For cards, show at least:

  - Severity / Priority
  - Target Environment
  - Failure Type
  - Attempt Count
  - Assigned To
  - Tags

  Use tags only for lightweight filtering:

  - agent:run
  - azure / databricks
  - SEV1 / SEV2 / SEV3

  Queries / Views
  You’ll want saved queries, not just one board:

  - Active Agent Incidents
  - Awaiting Approval
  - Escalated Last 7 Days
  - Closed - Resolved
  - Closed - Escalated

  Card Styling
  Use card styles to make the board readable at a glance:

  - Red for SEV1
  - Amber for Awaiting Approval
  - Gray for Agent Outcome = Escalated
  - Blue tag color for databricks
  - Green tag color for azure

  One Gap In The Current Code
  Right now the real lifecycle is tracked in Neon and comments, but I don’t see the worker actually calling AdoClient.update_state() from integrations/ado.py:119. So if you configure the board this way
  today, cards won’t reliably move columns unless you also start syncing System.State from the worker.

  So the practical answer is:

  - one dedicated team board
  - one incident WIT
  - four columns: New, Investigating, Awaiting Approval, Closed
  - one custom outcome field for Resolved vs Escalated
  - custom fields for environment/failure metadata
  - saved queries for operational slices

  That is the shape I’d use.

  Sources:

  - Microsoft Learn: board columns map to workflow states, unmapped states don’t appear, and Completed maps only to the last column:
    https://learn.microsoft.com/en-us/azure/devops/boards/boards/add-columns?view=azure-devops
  - Microsoft Learn: only one state can exist in the Completed category for inherited processes:
    https://learn.microsoft.com/en-us/azure/devops/organizations/settings/work/customize-process-workflow?view=azure-devops
  - Microsoft Learn: custom WITs and fields in inherited processes: https://learn.microsoft.com/en-us/azure/devops/organizations/settings/work/customize-process-work-item-type?view=azure-devops
  - Microsoft Learn: board cards can show custom fields and style rules: https://learn.microsoft.com/en-us/azure/devops/boards/boards/customize-cards?view=azure-devops
  - Microsoft Learn: each team has its own board, and sharing items across boards can produce unexpected board column values:
    https://learn.microsoft.com/en-us/azure/devops/boards/boards/kanban-quickstart?view=azure-devops

  If you want, I can turn this into an exact Azure DevOps process spec: custom fields, state names, board columns, tag rules, and the code changes needed to keep the board in sync automatically.
