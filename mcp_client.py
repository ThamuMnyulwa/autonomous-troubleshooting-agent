"""
MCP tool loader.

This module builds MCP connection configs for Azure DevOps, Azure, and Databricks,
then asks `langchain-mcp-adapters` to create LangChain tools from them.

For the installed adapter version, `MultiServerMCPClient.get_tools()` is the
supported API. The returned tools create a fresh MCP session per invocation,
which avoids holding orphaned client sessions after tool discovery.
"""

from __future__ import annotations

from urllib.parse import urlparse

import structlog
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from config import settings

log = structlog.get_logger()


VALID_TARGET_ENVS = {"databricks", "azure", "both"}


def _normalize_https_url(url: str, *, setting_name: str) -> str:
    normalized = url.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        msg = f"{setting_name} must be a full http(s) URL: {url!r}"
        raise ValueError(msg)
    return normalized


def resolve_databricks_mcp_url() -> str:
    """
    Resolve the Databricks MCP endpoint explicitly.

    Production should not guess which Databricks MCP gateway is deployed, so the
    endpoint must be provided via configuration when Databricks tooling is used.
    """
    if not settings.databricks_mcp_url.strip():
        msg = (
            "DATABRICKS_MCP_URL must be set when target_env includes databricks. "
            "The worker does not assume which Databricks MCP endpoint is deployed."
        )
        raise ValueError(msg)
    return _normalize_https_url(
        settings.databricks_mcp_url,
        setting_name="DATABRICKS_MCP_URL",
    )


def _databricks_server_config() -> dict:
    url = resolve_databricks_mcp_url()
    return {
        "transport": "streamable_http",
        "url": url,
        "headers": {
            "Authorization": f"Bearer {settings.databricks_token}",
        },
    }


def _azure_server_config() -> dict:
    env: dict[str, str] = {}
    if settings.azure_tenant_id:
        env["AZURE_TENANT_ID"] = settings.azure_tenant_id
    if settings.azure_client_id:
        env["AZURE_CLIENT_ID"] = settings.azure_client_id
    if settings.azure_client_secret:
        env["AZURE_CLIENT_SECRET"] = settings.azure_client_secret

    return {
        "command": "npx",
        "args": ["-y", "@azure/mcp", "server", "start"],
        "env": env,
        "transport": "stdio",
    }


def _ado_server_config() -> dict:
    return {
        "command": "npx",
        "args": [
            "-y",
            "@azure-devops/mcp",
            _ado_org_name(settings.ado_org_url),
            "-d",
            "core",
            "work-items",
            "repositories",
        ],
        "env": {
            "AZURE_DEVOPS_EXT_PAT": settings.ado_pat,
        },
        "transport": "stdio",
    }


def _ado_org_name(org_url: str) -> str:
    parsed = urlparse(org_url)
    if parsed.netloc.endswith(".visualstudio.com"):
        return parsed.netloc.split(".")[0]

    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts:
        return path_parts[0]

    msg = f"Unable to derive Azure DevOps organization name from URL: {org_url}"
    raise ValueError(msg)


def _server_configs(target_env: str) -> dict[str, dict]:
    if target_env not in VALID_TARGET_ENVS:
        msg = f"Unsupported target_env={target_env!r}; expected one of {sorted(VALID_TARGET_ENVS)}"
        raise ValueError(msg)

    servers: dict[str, dict] = {
        "ado": _ado_server_config(),
    }

    if target_env in ("databricks", "both"):
        servers["databricks"] = _databricks_server_config()

    if target_env in ("azure", "both"):
        servers["azure"] = _azure_server_config()

    return servers


async def load_mcp_tools(target_env: str = "databricks") -> list[BaseTool]:
    """
    Discover MCP tools for the requested environment.

    Tool names are prefixed with the server name to avoid collisions across ADO,
    Azure, and Databricks.
    """
    servers = _server_configs(target_env)
    log.info("mcp.connecting", servers=list(servers.keys()))

    client = MultiServerMCPClient(servers, tool_name_prefix=True)
    tools = await client.get_tools()

    log.info("mcp.tools_loaded", count=len(tools), tools=[t.name for t in tools])
    return tools
