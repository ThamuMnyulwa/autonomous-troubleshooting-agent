from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _use_local_mlflow_prompt_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    monkeypatch.setenv("MLFLOW_REGISTRY_URI", uri)
    monkeypatch.setenv("PROMPT_REGISTRY_ACTIVE_ALIAS", "active")
    monkeypatch.setenv("PROMPT_REGISTRY_AUTO_SEED", "true")

    try:
        import prompts

        prompts.registry._initialized = False
        prompts.registry._client = None
    except Exception:
        pass


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        parts = Path(str(item.fspath)).parts
        if "integration" in parts:
            item.add_marker(pytest.mark.integration)
            continue
        if "unit" in parts:
            item.add_marker(pytest.mark.unit)
