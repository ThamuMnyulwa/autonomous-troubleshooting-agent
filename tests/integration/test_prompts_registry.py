from __future__ import annotations

import prompts as prompts_module
from prompts.templates import build_default_registry


def test_mlflow_prompt_registry_bootstraps_local_aliases() -> None:
    registry = build_default_registry()

    system_prompt = registry.get("system")
    pinned_prompt = registry.get("system", "1.0.0")

    assert system_prompt.version == "1.1.0"
    assert system_prompt.registry_version == 2
    assert pinned_prompt.registry_version == 1
    assert system_prompt.uri == "prompts:/system/2"


def test_mlflow_prompt_registry_renders_prompt_builders(monkeypatch) -> None:
    monkeypatch.setattr(prompts_module, "registry", build_default_registry())

    text = prompts_module.build_investigation_prompt(
        work_item={
            "id": 42,
            "fields": {
                "System.Title": "Warehouse failure",
                "System.Tags": "agent:run;databricks",
                "System.Description": (
                    "<p>Query failed</p><!-- agent-metadata\n"
                    "environment: prod\n"
                    "incident_target: warehouse/main\n"
                    "target_resource_name: warehouse/main\n"
                    "-->"
                ),
            },
        },
        comments=[],
        attempt=0,
    )

    assert "# Pipeline Failure Investigation - Attempt 1" in text
    assert "**Work Item:** #42 - Warehouse failure" in text
    assert "**Environment:** prod" in text
    assert "## Exact Incident Target" in text
    assert "`warehouse/main`" in text
    assert "Do not substitute similarly named resources" in text


def test_mlflow_prompt_registry_uses_active_alias_for_unpinned_prompt(monkeypatch) -> None:
    registry = build_default_registry()
    monkeypatch.setenv("PROMPT_REGISTRY_ACTIVE_ALIAS", "v1_0_0")

    assert registry.get("system").version == "1.0.0"
