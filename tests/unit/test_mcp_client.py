from __future__ import annotations

from types import SimpleNamespace

import pytest

import mcp_client


def test_databricks_server_config_uses_explicit_mcp_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_client,
        "settings",
        SimpleNamespace(
            databricks_mcp_url="https://adb.example.com/api/2.0/mcp/sql",
            databricks_token="token-123",
        ),
    )

    config = mcp_client._databricks_server_config()

    assert config["url"] == "https://adb.example.com/api/2.0/mcp/sql"
    assert config["headers"]["Authorization"] == "Bearer token-123"


def test_databricks_server_config_requires_explicit_mcp_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_client,
        "settings",
        SimpleNamespace(databricks_mcp_url="  "),
    )

    with pytest.raises(ValueError, match="DATABRICKS_MCP_URL must be set"):
        mcp_client.resolve_databricks_mcp_url()
