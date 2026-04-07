from __future__ import annotations

import importlib
import sys

import pytest


def test_config_import_is_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GOOGLE_API_KEY",
        "DATABASE_URL",
        "ADO_ORG_URL",
        "ADO_PROJECT",
        "ADO_PAT",
        "ADO_SERVICE_HOOK_SECRET",
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    sys.modules.pop("config", None)
    config = importlib.import_module("config")

    with pytest.raises(Exception):
        _ = config.settings.google_api_key


def test_settings_default_to_local_mlflow_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://agent:agent@localhost/agent")
    monkeypatch.setenv("ADO_ORG_URL", "https://dev.azure.com/example")
    monkeypatch.setenv("ADO_PROJECT", "project")
    monkeypatch.setenv("ADO_PAT", "pat")
    monkeypatch.setenv("ADO_SERVICE_HOOK_SECRET", "secret")
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("MLFLOW_REGISTRY_URI", raising=False)

    sys.modules.pop("config", None)
    config = importlib.import_module("config")
    settings = config.Settings()

    assert (
        settings.mlflow_tracking_uri
        == "postgresql+psycopg://mlflow:mlflow@127.0.0.1:54329/mlflow"
    )
    assert (
        settings.mlflow_server_backend_store_uri
        == "postgresql+psycopg://mlflow:mlflow@127.0.0.1:54329/mlflow"
    )
    assert settings.mlflow_server_uri == "http://127.0.0.1:5000"
    assert settings.mlflow_artifact_root == "./.mlflow/artifacts"


def test_prompt_registry_default_tracking_uri_targets_local_mlflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)

    registry_module = importlib.import_module("prompts.registry")

    assert (
        registry_module._tracking_uri()
        == "postgresql+psycopg://mlflow:mlflow@127.0.0.1:54329/mlflow"
    )
