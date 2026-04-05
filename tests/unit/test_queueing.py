from __future__ import annotations

import pytest

from queueing import build_run_message, normalize_run_message


def test_build_run_message_maps_run_fields() -> None:
    payload = build_run_message(
        {
            "work_item_id": 42,
            "run_id": "run-42",
            "attempt_count": 1,
            "thread_id": "work_item_42_attempt_1",
        },
        action="resume",
    )

    assert payload == {
        "work_item_id": 42,
        "run_id": "run-42",
        "attempt": 1,
        "thread_id": "work_item_42_attempt_1",
        "action": "resume",
    }


def test_normalize_run_message_rejects_unknown_action() -> None:
    with pytest.raises(ValueError, match="Unsupported queue action"):
        normalize_run_message({"work_item_id": 42, "action": "cleanup"})
