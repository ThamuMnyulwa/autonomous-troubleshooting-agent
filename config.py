"""
Central configuration - loaded from environment variables or .env file.
In production (Azure Container Apps / Azure Functions) values are injected from
Key Vault-backed secrets or managed environment configuration.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM ──────────────────────────────────────────────────────────────────
    google_api_key: str = Field(
        "",
        description="Gemini API key from Google AI Studio (required for worker, not intake)",
    )
    gemini_model: str = Field(
        "gemini-2.5-flash",
        description="Model to use for the agent",
    )
    gemini_temperature: float = Field(
        0.1,
        description="Never use 0.0; Gemini tool calling is unreliable there.",
    )

    # ── Neon (PostgreSQL) ────────────────────────────────────────────────────
    database_url: str = Field(
        ...,
        description="Neon connection string - must include sslmode=require",
        # Example: postgresql://user:pass@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require
    )
    db_pool_max_size: int = Field(5, description="Neon free tier connection limit")
    db_pool_min_size: int = Field(1)

    # ── Azure DevOps ─────────────────────────────────────────────────────────
    ado_org_url: str = Field(
        ...,
        description="e.g. https://dev.azure.com/your-org",
    )
    ado_project: str = Field(..., description="ADO project name")
    ado_pat: str = Field(..., description="Fine-grained PAT: Work Items read/write, Code read")
    ado_service_hook_secret: str = Field(
        ...,
        description="Shared secret for validating ADO Service Hook HMAC signatures",
    )
    ado_area_path: str = Field("", description="Optional area path filter for agent work items")
    ado_work_item_type: str = Field(
        "Issue",
        description="Work item type for agent incidents. 'Bug' for Agile/Scrum, 'Issue' for Basic.",
    )
    ado_state_queued: str = Field(
        "",
        description=(
            "Optional ADO System.State used when the agent creates or re-queues an incident. "
            "Defaults by work item type if unset."
        ),
    )
    ado_state_investigating: str = Field(
        "",
        description=(
            "Optional ADO System.State used while the agent is investigating. "
            "Defaults by work item type if unset."
        ),
    )
    ado_state_awaiting_approval: str = Field(
        "",
        description=(
            "Optional ADO System.State used when waiting for human approval. "
            "Defaults by work item type if unset."
        ),
    )
    ado_state_resolved: str = Field(
        "",
        description=(
            "Optional ADO System.State used when the agent resolves an incident. "
            "Defaults by work item type if unset."
        ),
    )
    ado_state_escalated: str = Field(
        "",
        description=(
            "Optional ADO System.State used when the agent escalates an incident. "
            "Defaults by work item type if unset."
        ),
    )

    # ── Databricks ───────────────────────────────────────────────────────────
    databricks_host: str = Field(
        "",
        description="e.g. https://adb-xxx.azuredatabricks.net (required for worker)",
    )
    databricks_token: str = Field(
        "",
        description="Service principal PAT or M2M OAuth token (required for worker)",
    )
    databricks_warehouse_id: str = Field(
        "",
        description="SQL warehouse ID for system table queries",
    )
    databricks_mcp_url: str = Field(
        "",
        description=(
            "Explicit Databricks MCP endpoint used by the worker, for example "
            "https://<workspace>/api/2.0/mcp/sql or a dedicated MCP gateway URL."
        ),
    )

    # ── Azure ────────────────────────────────────────────────────────────────
    azure_tenant_id: str = Field("", description="Entra ID tenant for Azure MCP auth")
    azure_client_id: str = Field("", description="Service principal client ID")
    azure_client_secret: str = Field("", description="Service principal secret (or use WIF)")

    # ── Azure Service Bus ────────────────────────────────────────────────────
    azure_service_bus_namespace: str = Field(
        "",
        description=(
            "Fully-qualified Service Bus namespace, e.g. "
            "my-namespace.servicebus.windows.net. Used with Managed Identity."
        ),
    )
    azure_service_bus_connection_string: str = Field(
        "",
        description="Optional Service Bus connection string for local/dev use.",
    )
    azure_service_bus_queue_name: str = Field(
        "agent-runs",
        description="Queue name for investigation and resume run envelopes.",
    )
    azure_service_bus_receive_wait_seconds: int = Field(
        5,
        description="How long the worker waits for a queue message before exiting.",
    )
    azure_service_bus_lock_renewal_seconds: int = Field(
        3600,
        description="Maximum lock renewal window for a single long-running worker execution.",
    )

    # ── MLflow prompt registry ────────────────────────────────────────────────
    mlflow_tracking_uri: str = Field(
        "postgresql+psycopg://mlflow:mlflow@127.0.0.1:54329/mlflow",
        description=(
            "MLflow tracking URI used by the prompt registry. For local development we default "
            "to a direct connection to the local PostgreSQL-backed MLflow store."
        ),
    )
    mlflow_registry_uri: str = Field(
        "",
        description="Optional explicit MLflow registry URI. Defaults to mlflow_tracking_uri.",
    )
    mlflow_server_backend_store_uri: str = Field(
        "postgresql+psycopg://mlflow:mlflow@127.0.0.1:54329/mlflow",
        description=(
            "Local/backend MLflow metadata database URI. Used when starting a self-hosted "
            "MLflow server for prompt registry development."
        ),
    )
    mlflow_server_uri: str = Field(
        "http://127.0.0.1:5000",
        description="Optional local MLflow server UI URI for manual inspection.",
    )
    mlflow_artifact_root: str = Field(
        "./.mlflow/artifacts",
        description=(
            "Default artifact root for the local MLflow server. Keep this on the local "
            "filesystem for now."
        ),
    )
    prompt_registry_active_alias: str = Field(
        "active",
        description="MLflow alias used by the runtime when no prompt version is pinned.",
    )
    prompt_registry_auto_seed: bool = Field(
        True,
        description="Register bundled prompt definitions into MLflow on first use if missing.",
    )
    mlflow_runtime_tracing_enabled: bool = Field(
        True,
        description="Emit MLflow runtime traces for worker and graph execution.",
    )
    mlflow_tracing_experiment_name: str = Field(
        "pipeline-resolver-runtime",
        description="MLflow experiment used to persist runtime traces.",
    )

    # ── Agent behaviour ──────────────────────────────────────────────────────
    max_iterations: int = Field(5, description="Hard stop on ReAct loop - prevents runaway costs")
    azure_max_iterations: int = Field(
        8,
        description=(
            "Higher reasoning budget for Azure-only investigations, which often need several "
            "discovery hops before reaching a concrete diagnosis."
        ),
    )
    post_write_followup_iterations: int = Field(
        2,
        description=(
            "Extra reasoning steps allowed after an approved write executes so the agent can "
            "confirm outcome instead of escalating immediately at the base limit."
        ),
    )
    max_attempts: int = Field(3, description="Max HITL retry attempts before escalation")
    checkpoint_ttl_days: int = Field(7, description="Days to retain LangGraph checkpoints in Neon")

    # ── Intake API ───────────────────────────────────────────────────────────
    intake_port: int = Field(8080)
    log_level: str = Field("INFO")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings lazily so imports don't fail before env vars are present."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings instance. Useful in tests."""
    get_settings.cache_clear()


class _SettingsProxy:
    """Thin lazy proxy that preserves the existing `settings.foo` call sites."""

    def __getattr__(self, item: str):
        return getattr(get_settings(), item)


settings = _SettingsProxy()
