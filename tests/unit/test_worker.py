from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from agents import worker
from db import RunStatus


class _FakeAdoClient:
    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []
        self.states: list[tuple[int, str]] = []

    async def __aenter__(self) -> _FakeAdoClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get_work_item(self, work_item_id: int) -> dict:
        return {
            "id": work_item_id,
            "url": "https://ado.example/items/42",
            "target_env": "azure",
            "failure_type": "timeout",
            "fields": {},
        }

    async def get_comments(self, _: int) -> list[dict]:
        return []

    async def add_comment(self, work_item_id: int, text: str) -> dict:
        self.comments.append((work_item_id, text))
        return {"text": text}

    async def update_state(self, work_item_id: int, state: str) -> dict:
        self.states.append((work_item_id, state))
        return {"state": state}


class _FakeCompiledGraph:
    async def ainvoke(self, state, _config):
        assert state is not None
        return {
            "status": RunStatus.ESCALATED.value,
            "messages": [AIMessage(content="Unable to continue safely.")],
            "blocked_tools": [],
            "evidence": [],
        }

    async def aget_state(self, _config):
        return SimpleNamespace(next=[])


class _FakeGraph:
    def compile(self, **_kwargs) -> _FakeCompiledGraph:
        return _FakeCompiledGraph()


class _FakeResumeCompiledGraph:
    async def ainvoke(self, state, _config):
        assert state is None
        return {
            "status": RunStatus.INVESTIGATING.value,
            "messages": [AIMessage(content="I am still looking into the incident.")],
            "blocked_tools": [],
            "evidence": [],
            "write_executed": False,
        }

    async def aget_state(self, _config):
        return SimpleNamespace(next=["execute_write"])


class _FakeResumeGraph:
    def compile(self, **_kwargs) -> _FakeResumeCompiledGraph:
        return _FakeResumeCompiledGraph()


class _FakeDiagnosisCompiledGraph:
    async def ainvoke(self, state, _config):
        assert state is not None
        return {
            "status": RunStatus.ESCALATED.value,
            "messages": [
                AIMessage(
                    content=(
                        "Most Likely Root Cause: the downstream dependency is timing out. "
                        "Next step: verify the dependency endpoint configuration."
                    )
                )
            ],
            "blocked_tools": [],
            "evidence": [
                {
                    "source": "azure_group_list",
                    "summary": "Located the Azure resource group for the failing job.",
                },
                {
                    "source": "azure_applens",
                    "summary": "AppLens reported repeated downstream timeout symptoms.",
                },
            ],
            "pending_tool_call": None,
            "write_executed": False,
        }

    async def aget_state(self, _config):
        return SimpleNamespace(next=[])


class _FakeDiagnosisGraph:
    def compile(self, **_kwargs) -> _FakeDiagnosisCompiledGraph:
        return _FakeDiagnosisCompiledGraph()


class _FakeRecoveredCompiledGraph:
    async def ainvoke(self, state, _config):
        assert state is not None
        return {
            "status": RunStatus.RESOLVED.value,
            "messages": [
                AIMessage(
                    content="The alert has cleared and no further remediation is required."
                )
            ],
            "blocked_tools": [],
            "evidence": [
                {
                    "source": "azure_applens",
                    "summary": "Health checks now report the job as recovered.",
                }
            ],
            "pending_tool_call": None,
            "write_executed": False,
        }

    async def aget_state(self, _config):
        return SimpleNamespace(next=[])


class _FakeRecoveredGraph:
    def compile(self, **_kwargs) -> _FakeRecoveredCompiledGraph:
        return _FakeRecoveredCompiledGraph()


class _FakePendingApprovalCompiledGraph:
    async def ainvoke(self, state, _config):
        assert state is not None
        return {
            "status": RunStatus.AWAITING_APPROVAL.value,
            "messages": [AIMessage(content="I need approval for the full tool batch.")],
            "blocked_tools": [],
            "evidence": [],
            "pending_tool_call": {
                "name": "ado_wit_add_comment",
                "args": {"work_item_id": 42, "text": "note"},
            },
            "pending_tool_calls": [
                {"name": "azure_keyvault", "args": {"learn": True}},
                {
                    "name": "ado_wit_add_comment",
                    "args": {"work_item_id": 42, "text": "note"},
                },
            ],
            "write_executed": False,
        }

    async def aget_state(self, _config):
        return SimpleNamespace(next=[])


class _FakePendingApprovalGraph:
    def compile(self, **_kwargs) -> _FakePendingApprovalCompiledGraph:
        return _FakePendingApprovalCompiledGraph()


class _NoCheckpointCompiledGraph:
    async def ainvoke(self, _state, _config):
        raise AssertionError("ainvoke should not run without a pending checkpoint")

    async def aget_state(self, _config):
        return SimpleNamespace(next=[])


class _NoCheckpointGraph:
    def compile(self, **_kwargs) -> _NoCheckpointCompiledGraph:
        return _NoCheckpointCompiledGraph()


class _ExplodingCompiledGraph:
    async def ainvoke(self, _state, _config):
        raise RuntimeError("tool transport crashed")

    async def aget_state(self, _config):
        return SimpleNamespace(next=[])


class _ExplodingGraph:
    def compile(self, **_kwargs) -> _ExplodingCompiledGraph:
        return _ExplodingCompiledGraph()


@pytest.mark.asyncio
async def test_run_agent_persists_work_item_context(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    state_calls: list[str] = []

    async def fake_get_run(work_item_id: int):
        assert work_item_id == 42
        return {
            "run_id": "run-42",
            "work_item_id": 42,
            "thread_id": "work_item_42_attempt_0",
            "attempt_count": 0,
            "status": RunStatus.QUEUED.value,
            "target_env": None,
            "failure_type": None,
        }

    async def fake_ensure_run(**kwargs):
        captured.update(kwargs)
        return {
            "run_id": "run-42",
            "work_item_id": 42,
            "thread_id": "work_item_42_attempt_0",
            "attempt_count": 0,
            "status": RunStatus.INVESTIGATING.value,
            "target_env": kwargs["target_env"],
            "failure_type": kwargs["failure_type"],
        }

    async def fake_update_status(
        _work_item_id: int,
        _status: RunStatus,
        _proposed_fix_url=None,
    ) -> None:
        return None

    async def fake_bootstrap(_checkpointer) -> None:
        return None

    async def fake_load_mcp_tools(*, target_env: str):
        assert target_env == "azure"
        return []

    def fake_ado_state_for_stage(stage: str) -> str:
        state_calls.append(stage)
        return {
            "investigating": "Custom Investigating",
            "awaiting_approval": "Custom Approval",
            "resolved": "Custom Resolved",
            "escalated": "Custom Escalated",
        }[stage]

    @asynccontextmanager
    async def fake_get_checkpointer():
        yield object()

    monkeypatch.setattr(worker, "get_run", fake_get_run)
    monkeypatch.setattr(worker, "ensure_run", fake_ensure_run)
    monkeypatch.setattr(worker, "update_status", fake_update_status)
    monkeypatch.setattr(worker, "bootstrap", fake_bootstrap)
    monkeypatch.setattr(worker, "load_mcp_tools", fake_load_mcp_tools)
    monkeypatch.setattr(worker, "build_graph", lambda _tools: _FakeGraph())
    monkeypatch.setattr(worker, "get_checkpointer", fake_get_checkpointer)
    monkeypatch.setattr(worker, "AdoClient", _FakeAdoClient)
    monkeypatch.setattr(worker, "ado_state_for_stage", fake_ado_state_for_stage)
    monkeypatch.setattr(worker, "settings", SimpleNamespace(max_attempts=3))

    await worker.run_agent(42)

    assert captured == {
        "work_item_id": 42,
        "work_item_url": "https://ado.example/items/42",
        "target_env": "azure",
        "failure_type": "timeout",
    }
    assert state_calls == ["investigating", "escalated"]


@pytest.mark.asyncio
async def test_run_agent_resume_escalates_if_graph_does_not_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ado_client = _FakeAdoClient()
    status_updates: list[RunStatus] = []

    async def fake_get_run(work_item_id: int):
        assert work_item_id == 42
        return {
            "run_id": "run-42",
            "work_item_id": 42,
            "thread_id": "work_item_42_attempt_0",
            "attempt_count": 0,
            "status": RunStatus.AWAITING_APPROVAL.value,
            "target_env": "azure",
            "failure_type": "timeout",
        }

    async def fake_ensure_run(**kwargs):
        return {
            "run_id": "run-42",
            "work_item_id": 42,
            "thread_id": "work_item_42_attempt_0",
            "attempt_count": 0,
            "status": RunStatus.INVESTIGATING.value,
            "target_env": kwargs["target_env"],
            "failure_type": kwargs["failure_type"],
        }

    async def fake_update_status(
        _work_item_id: int,
        status: RunStatus,
        _proposed_fix_url=None,
    ) -> None:
        status_updates.append(status)

    async def fake_bootstrap(_checkpointer) -> None:
        return None

    async def fake_load_mcp_tools(*, target_env: str):
        assert target_env == "azure"
        return []

    @asynccontextmanager
    async def fake_get_checkpointer():
        yield object()

    monkeypatch.setattr(worker, "get_run", fake_get_run)
    monkeypatch.setattr(worker, "ensure_run", fake_ensure_run)
    monkeypatch.setattr(worker, "update_status", fake_update_status)
    monkeypatch.setattr(worker, "bootstrap", fake_bootstrap)
    monkeypatch.setattr(worker, "load_mcp_tools", fake_load_mcp_tools)
    monkeypatch.setattr(worker, "build_graph", lambda _tools: _FakeResumeGraph())
    monkeypatch.setattr(worker, "get_checkpointer", fake_get_checkpointer)
    monkeypatch.setattr(worker, "AdoClient", lambda: ado_client)
    monkeypatch.setattr(
        worker,
        "ado_state_for_stage",
        lambda stage: {
            "investigating": "Custom Investigating",
            "escalated": "Custom Escalated",
        }[stage],
    )
    monkeypatch.setattr(worker, "settings", SimpleNamespace(max_attempts=3))

    await worker.run_agent(42, action="resume")

    assert status_updates == [RunStatus.INVESTIGATING, RunStatus.ESCALATED]
    assert any("did not reach a resolved terminal state" in text for _, text in ado_client.comments)
    assert ado_client.states == [(42, "Custom Investigating"), (42, "Custom Escalated")]


@pytest.mark.asyncio
async def test_run_agent_escalates_on_graph_execution_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ado_client = _FakeAdoClient()
    status_updates: list[RunStatus] = []

    async def fake_get_run(work_item_id: int):
        assert work_item_id == 42
        return {
            "run_id": "run-42",
            "work_item_id": 42,
            "thread_id": "work_item_42_attempt_0",
            "attempt_count": 0,
            "status": RunStatus.QUEUED.value,
            "target_env": "azure",
            "failure_type": "timeout",
        }

    async def fake_ensure_run(**kwargs):
        return {
            "run_id": "run-42",
            "work_item_id": 42,
            "thread_id": "work_item_42_attempt_0",
            "attempt_count": 0,
            "status": RunStatus.INVESTIGATING.value,
            "target_env": kwargs["target_env"],
            "failure_type": kwargs["failure_type"],
        }

    async def fake_update_status(
        _work_item_id: int,
        status: RunStatus,
        _proposed_fix_url=None,
    ) -> None:
        status_updates.append(status)

    async def fake_bootstrap(_checkpointer) -> None:
        return None

    async def fake_load_mcp_tools(*, target_env: str):
        assert target_env == "azure"
        return []

    @asynccontextmanager
    async def fake_get_checkpointer():
        yield object()

    monkeypatch.setattr(worker, "get_run", fake_get_run)
    monkeypatch.setattr(worker, "ensure_run", fake_ensure_run)
    monkeypatch.setattr(worker, "update_status", fake_update_status)
    monkeypatch.setattr(worker, "bootstrap", fake_bootstrap)
    monkeypatch.setattr(worker, "load_mcp_tools", fake_load_mcp_tools)
    monkeypatch.setattr(worker, "build_graph", lambda _tools: _ExplodingGraph())
    monkeypatch.setattr(worker, "get_checkpointer", fake_get_checkpointer)
    monkeypatch.setattr(worker, "AdoClient", lambda: ado_client)
    monkeypatch.setattr(
        worker,
        "ado_state_for_stage",
        lambda stage: {
            "investigating": "Custom Investigating",
            "escalated": "Custom Escalated",
        }[stage],
    )
    monkeypatch.setattr(worker, "settings", SimpleNamespace(max_attempts=3))

    await worker.run_agent(42)

    assert status_updates == [RunStatus.INVESTIGATING, RunStatus.ESCALATED]
    assert any("Runtime error: tool transport crashed" in text for _, text in ado_client.comments)
    assert ado_client.states == [(42, "Custom Investigating"), (42, "Custom Escalated")]


@pytest.mark.asyncio
async def test_run_agent_hands_off_diagnosis_only_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ado_client = _FakeAdoClient()
    status_updates: list[RunStatus] = []

    async def fake_get_run(work_item_id: int):
        assert work_item_id == 42
        return {
            "run_id": "run-42",
            "work_item_id": 42,
            "thread_id": "work_item_42_attempt_0",
            "attempt_count": 0,
            "status": RunStatus.QUEUED.value,
            "target_env": "azure",
            "failure_type": "timeout",
        }

    async def fake_ensure_run(**kwargs):
        return {
            "run_id": "run-42",
            "work_item_id": 42,
            "thread_id": "work_item_42_attempt_0",
            "attempt_count": 0,
            "status": RunStatus.INVESTIGATING.value,
            "target_env": kwargs["target_env"],
            "failure_type": kwargs["failure_type"],
        }

    async def fake_update_status(
        _work_item_id: int,
        status: RunStatus,
        _proposed_fix_url=None,
    ) -> None:
        status_updates.append(status)

    async def fake_bootstrap(_checkpointer) -> None:
        return None

    async def fake_load_mcp_tools(*, target_env: str):
        assert target_env == "azure"
        return []

    @asynccontextmanager
    async def fake_get_checkpointer():
        yield object()

    monkeypatch.setattr(worker, "get_run", fake_get_run)
    monkeypatch.setattr(worker, "ensure_run", fake_ensure_run)
    monkeypatch.setattr(worker, "update_status", fake_update_status)
    monkeypatch.setattr(worker, "bootstrap", fake_bootstrap)
    monkeypatch.setattr(worker, "load_mcp_tools", fake_load_mcp_tools)
    monkeypatch.setattr(worker, "build_graph", lambda _tools: _FakeDiagnosisGraph())
    monkeypatch.setattr(worker, "get_checkpointer", fake_get_checkpointer)
    monkeypatch.setattr(worker, "AdoClient", lambda: ado_client)
    monkeypatch.setattr(
        worker,
        "ado_state_for_stage",
        lambda stage: {
            "investigating": "Custom Investigating",
            "escalated": "Custom Escalated",
        }[stage],
    )
    monkeypatch.setattr(worker, "settings", SimpleNamespace(max_attempts=3))

    await worker.run_agent(42)

    assert status_updates == [RunStatus.INVESTIGATING, RunStatus.ESCALATED]
    assert any("## Agent Handoff" in text for _, text in ado_client.comments)
    assert any("### Methods tried" in text for _, text in ado_client.comments)
    assert any("### Recommended next step" in text for _, text in ado_client.comments)
    assert ado_client.states == [(42, "Custom Investigating"), (42, "Custom Escalated")]


@pytest.mark.asyncio
async def test_run_agent_marks_recovered_read_only_runs_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ado_client = _FakeAdoClient()
    status_updates: list[RunStatus] = []

    async def fake_get_run(work_item_id: int):
        assert work_item_id == 42
        return {
            "run_id": "run-42",
            "work_item_id": 42,
            "thread_id": "work_item_42_attempt_0",
            "attempt_count": 0,
            "status": RunStatus.QUEUED.value,
            "target_env": "azure",
            "failure_type": "timeout",
        }

    async def fake_ensure_run(**kwargs):
        return {
            "run_id": "run-42",
            "work_item_id": 42,
            "thread_id": "work_item_42_attempt_0",
            "attempt_count": 0,
            "status": RunStatus.INVESTIGATING.value,
            "target_env": kwargs["target_env"],
            "failure_type": kwargs["failure_type"],
        }

    async def fake_update_status(
        _work_item_id: int,
        status: RunStatus,
        _proposed_fix_url=None,
    ) -> None:
        status_updates.append(status)

    async def fake_bootstrap(_checkpointer) -> None:
        return None

    async def fake_load_mcp_tools(*, target_env: str):
        assert target_env == "azure"
        return []

    @asynccontextmanager
    async def fake_get_checkpointer():
        yield object()

    monkeypatch.setattr(worker, "get_run", fake_get_run)
    monkeypatch.setattr(worker, "ensure_run", fake_ensure_run)
    monkeypatch.setattr(worker, "update_status", fake_update_status)
    monkeypatch.setattr(worker, "bootstrap", fake_bootstrap)
    monkeypatch.setattr(worker, "load_mcp_tools", fake_load_mcp_tools)
    monkeypatch.setattr(worker, "build_graph", lambda _tools: _FakeRecoveredGraph())
    monkeypatch.setattr(worker, "get_checkpointer", fake_get_checkpointer)
    monkeypatch.setattr(worker, "AdoClient", lambda: ado_client)
    monkeypatch.setattr(
        worker,
        "ado_state_for_stage",
        lambda stage: {
            "investigating": "Custom Investigating",
            "resolved": "Custom Resolved",
        }[stage],
    )
    monkeypatch.setattr(worker, "settings", SimpleNamespace(max_attempts=3))

    await worker.run_agent(42)

    assert status_updates == [RunStatus.INVESTIGATING, RunStatus.RESOLVED]
    assert any("## Agent Diagnosis" in text for _, text in ado_client.comments)
    assert ado_client.states == [(42, "Custom Investigating"), (42, "Custom Resolved")]


def test_format_approval_request_lists_entire_tool_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "settings", SimpleNamespace(max_attempts=3))

    text = worker._format_approval_request(
        {
            "attempt_count": 0,
            "failure_type": "timeout",
            "evidence": [],
            "proposed_fix": None,
            "pending_tool_call": {
                "name": "ado_wit_add_comment",
                "args": {"work_item_id": 42, "text": "note"},
            },
            "pending_tool_calls": [
                {"name": "azure_keyvault", "args": {"learn": True}},
                {
                    "name": "ado_wit_add_comment",
                    "args": {"work_item_id": 42, "text": "note"},
                },
            ],
        },
        "Approval is required for the tool batch.",
    )

    assert "### Pending tool batch" in text
    assert "`azure_keyvault`" in text
    assert "`ado_wit_add_comment`" in text
    assert "proceed with the pending tool batch" in text


@pytest.mark.asyncio
async def test_run_agent_resume_without_checkpoint_escalates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ado_client = _FakeAdoClient()
    status_updates: list[RunStatus] = []

    async def fake_get_run(work_item_id: int):
        assert work_item_id == 42
        return {
            "run_id": "run-42",
            "work_item_id": 42,
            "thread_id": "work_item_42_attempt_0",
            "attempt_count": 0,
            "status": RunStatus.AWAITING_APPROVAL.value,
            "target_env": "azure",
            "failure_type": "timeout",
        }

    async def fake_ensure_run(**kwargs):
        return {
            "run_id": "run-42",
            "work_item_id": 42,
            "thread_id": "work_item_42_attempt_0",
            "attempt_count": 0,
            "status": RunStatus.INVESTIGATING.value,
            "target_env": kwargs["target_env"],
            "failure_type": kwargs["failure_type"],
        }

    async def fake_update_status(
        _work_item_id: int,
        status: RunStatus,
        _proposed_fix_url=None,
    ) -> None:
        status_updates.append(status)

    async def fake_bootstrap(_checkpointer) -> None:
        return None

    async def fake_load_mcp_tools(*, target_env: str):
        assert target_env == "azure"
        return []

    @asynccontextmanager
    async def fake_get_checkpointer():
        yield object()

    monkeypatch.setattr(worker, "get_run", fake_get_run)
    monkeypatch.setattr(worker, "ensure_run", fake_ensure_run)
    monkeypatch.setattr(worker, "update_status", fake_update_status)
    monkeypatch.setattr(worker, "bootstrap", fake_bootstrap)
    monkeypatch.setattr(worker, "load_mcp_tools", fake_load_mcp_tools)
    monkeypatch.setattr(worker, "build_graph", lambda _tools: _NoCheckpointGraph())
    monkeypatch.setattr(worker, "get_checkpointer", fake_get_checkpointer)
    monkeypatch.setattr(worker, "AdoClient", lambda: ado_client)
    monkeypatch.setattr(
        worker,
        "ado_state_for_stage",
        lambda stage: {
            "investigating": "Custom Investigating",
            "escalated": "Custom Escalated",
        }[stage],
    )
    monkeypatch.setattr(worker, "settings", SimpleNamespace(max_attempts=3))

    await worker.run_agent(42, action="resume")

    assert status_updates == [RunStatus.INVESTIGATING, RunStatus.ESCALATED]
    assert any(
        "No pending approval step was found to resume" in text
        for _, text in ado_client.comments
    )
    assert ado_client.states == [(42, "Custom Investigating"), (42, "Custom Escalated")]
