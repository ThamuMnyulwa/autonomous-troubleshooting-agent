from __future__ import annotations

from types import SimpleNamespace

import pytest

from glue.normaliser import detect_source, make_alert_dedup_key
from integrations import ado
from integrations.ado import AdoClient, ado_state_for_stage, build_bug_description


def test_make_alert_dedup_key_is_stable() -> None:
    assert make_alert_dedup_key("job-123", "schema_mismatch") == make_alert_dedup_key(
        "job-123",
        "schema_mismatch",
    )


def test_build_bug_description_carries_failure_type_and_escapes_html() -> None:
    html = build_bug_description(
        environment="databricks",
        workspace_url='https://adb.example.com/?x=<script>',
        job_id="job-123",
        run_id="run-456",
        failed_task="<bad-task>",
        error_message="<boom>",
        failure_type="schema_mismatch",
        incident_target="job-123/task-a",
    )

    assert "failure_type: schema_mismatch" in html
    assert "incident_target: job-123/task-a" in html
    assert "&lt;boom&gt;" in html
    assert "<script>" not in html


def test_detect_source_uses_headers_and_payload_shape() -> None:
    assert detect_source({"run": {}}, headers={}) == "databricks"
    assert detect_source(
        {"schemaId": "azureMonitorCommonAlertSchema"},
        headers={},
    ) == "azure"


def test_ado_state_defaults_follow_issue_workflow(monkeypatch) -> None:
    monkeypatch.setattr(
        ado,
        "settings",
        SimpleNamespace(
            ado_work_item_type="Issue",
            ado_state_queued="",
            ado_state_investigating="",
            ado_state_awaiting_approval="",
            ado_state_resolved="",
            ado_state_escalated="",
        ),
    )

    assert ado_state_for_stage("queued") == "To Do"
    assert ado_state_for_stage("investigating") == "Doing"
    assert ado_state_for_stage("awaiting_approval") == "Doing"
    assert ado_state_for_stage("resolved") == "Done"
    assert ado_state_for_stage("escalated") == "Done"


def test_ado_state_defaults_follow_bug_workflow(monkeypatch) -> None:
    monkeypatch.setattr(
        ado,
        "settings",
        SimpleNamespace(
            ado_work_item_type="Bug",
            ado_state_queued="",
            ado_state_investigating="",
            ado_state_awaiting_approval="",
            ado_state_resolved="",
            ado_state_escalated="",
        ),
    )

    assert ado_state_for_stage("queued") == "New"
    assert ado_state_for_stage("investigating") == "Active"
    assert ado_state_for_stage("awaiting_approval") == "Active"
    assert ado_state_for_stage("resolved") == "Resolved"
    assert ado_state_for_stage("escalated") == "Resolved"


def test_ado_state_overrides_support_custom_workflow(monkeypatch) -> None:
    monkeypatch.setattr(
        ado,
        "settings",
        SimpleNamespace(
            ado_work_item_type="Agent Incident",
            ado_state_queued="Triage",
            ado_state_investigating="Investigating",
            ado_state_awaiting_approval="Awaiting Approval",
            ado_state_resolved="Closed",
            ado_state_escalated="Escalated",
        ),
    )

    assert ado_state_for_stage("queued") == "Triage"
    assert ado_state_for_stage("investigating") == "Investigating"
    assert ado_state_for_stage("awaiting_approval") == "Awaiting Approval"
    assert ado_state_for_stage("resolved") == "Closed"
    assert ado_state_for_stage("escalated") == "Escalated"


def test_ado_state_unknown_workflow_can_omit_initial_state(monkeypatch) -> None:
    monkeypatch.setattr(
        ado,
        "settings",
        SimpleNamespace(
            ado_work_item_type="Agent Incident",
            ado_state_queued="",
            ado_state_investigating="",
            ado_state_awaiting_approval="",
            ado_state_resolved="",
            ado_state_escalated="",
        ),
    )

    assert ado_state_for_stage("queued", allow_missing=True) is None


@pytest.mark.asyncio
async def test_create_bug_uses_mapped_issue_state(monkeypatch) -> None:
    captured: dict = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"id": 7}

    class _FakeHttpClient:
        async def post(self, _url: str, **kwargs) -> _FakeResponse:
            captured.update(kwargs)
            return _FakeResponse()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        ado,
        "settings",
        SimpleNamespace(
            ado_org_url="https://dev.azure.com/example",
            ado_project="proj",
            ado_pat="pat",
            ado_work_item_type="Issue",
            ado_state_queued="",
            ado_state_investigating="",
            ado_state_awaiting_approval="",
            ado_state_resolved="",
            ado_state_escalated="",
        ),
    )

    client = AdoClient()
    client._client = _FakeHttpClient()

    await client.create_bug("Test title", "<p>desc</p>", tags="agent:run")

    state_ops = [op for op in captured["json"] if op["path"] == "/fields/System.State"]
    assert state_ops == [{"op": "add", "path": "/fields/System.State", "value": "To Do"}]


@pytest.mark.asyncio
async def test_create_bug_omits_initial_state_for_unknown_workflow(monkeypatch) -> None:
    captured: dict = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"id": 8}

    class _FakeHttpClient:
        async def post(self, _url: str, **kwargs) -> _FakeResponse:
            captured.update(kwargs)
            return _FakeResponse()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        ado,
        "settings",
        SimpleNamespace(
            ado_org_url="https://dev.azure.com/example",
            ado_project="proj",
            ado_pat="pat",
            ado_work_item_type="Agent Incident",
            ado_state_queued="",
            ado_state_investigating="",
            ado_state_awaiting_approval="",
            ado_state_resolved="",
            ado_state_escalated="",
        ),
    )

    client = AdoClient()
    client._client = _FakeHttpClient()

    await client.create_bug("Test title", "<p>desc</p>", tags="agent:run")

    state_ops = [op for op in captured["json"] if op["path"] == "/fields/System.State"]
    assert state_ops == []
