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
