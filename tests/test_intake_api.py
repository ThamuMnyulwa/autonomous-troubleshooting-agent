from __future__ import annotations

import hashlib

import pytest

from intake import api


def test_parse_comment_action() -> None:
    assert api.parse_comment_action("approve") == "approve"
    assert api.parse_comment_action("This did not work") == "retry"
    assert api.parse_comment_action("just taking notes") == "ignore"


def test_extract_event_id_falls_back_to_payload_hash() -> None:
    body = b'{"hello":"world"}'
    assert api._extract_event_id({}, body) == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
async def test_handle_commented_queues_approval_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict, str]] = []

    async def fake_begin_approval_resume(work_item_id: int):
        assert work_item_id == 42
        return {"run_id": "run-1", "work_item_id": 42, "thread_id": "thread", "attempt_count": 0}

    async def fake_enqueue(run: dict, *, action: str) -> None:
        calls.append(("enqueue", run, action))

    monkeypatch.setattr(api, "begin_approval_resume", fake_begin_approval_resume)
    monkeypatch.setattr(api, "_enqueue", fake_enqueue)

    payload = {"resource": {"workItemId": 42, "comment": {"text": "approve"}}}
    await api._handle_commented(payload)

    assert calls == [
        (
            "enqueue",
            {"run_id": "run-1", "work_item_id": 42, "thread_id": "thread", "attempt_count": 0},
            "resume",
        )
    ]


@pytest.mark.asyncio
async def test_handle_commented_queues_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict, str]] = []

    async def fake_queue_retry(work_item_id: int):
        assert work_item_id == 42
        return {"run_id": "run-2", "work_item_id": 42, "thread_id": "thread2", "attempt_count": 1}

    async def fake_enqueue(run: dict, *, action: str) -> None:
        calls.append(("enqueue", run, action))

    monkeypatch.setattr(api, "queue_retry", fake_queue_retry)
    monkeypatch.setattr(api, "_enqueue", fake_enqueue)

    payload = {"resource": {"workItemId": 42, "comment": {"text": "did not work"}}}
    await api._handle_commented(payload)

    assert calls == [
        (
            "enqueue",
            {"run_id": "run-2", "work_item_id": 42, "thread_id": "thread2", "attempt_count": 1},
            "investigate",
        )
    ]
