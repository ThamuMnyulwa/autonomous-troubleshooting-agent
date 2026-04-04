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


@pytest.mark.asyncio
async def test_run_agent_persists_work_item_context(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

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
    monkeypatch.setattr(worker, "settings", SimpleNamespace(max_attempts=3))

    await worker.run_agent(42)

    assert captured == {
        "work_item_id": 42,
        "work_item_url": "https://ado.example/items/42",
        "target_env": "azure",
        "failure_type": "timeout",
    }
