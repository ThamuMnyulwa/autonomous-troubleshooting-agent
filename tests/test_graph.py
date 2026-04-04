from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agents.graph import classify_tool_call, classify_tool_calls, collect_recent_tool_messages


def test_classify_tool_call_prefers_metadata_read_only() -> None:
    tool = SimpleNamespace(name="ado_wit_get_work_item", metadata={"readOnlyHint": True})
    assert classify_tool_call(tool, {}) == "READ"


def test_classify_tool_call_blocks_destructive_sql() -> None:
    tool = SimpleNamespace(name="databricks_execute_sql", metadata={})
    assert classify_tool_call(tool, {"sql": "DROP TABLE prod.users"}) == "HARD_BLOCK"


def test_classify_tool_call_marks_non_read_sql_as_write() -> None:
    tool = SimpleNamespace(name="databricks_execute_sql", metadata={})
    assert (
        classify_tool_call(tool, {"sql": "INSERT INTO audit.events VALUES (1)"}) == "WRITE"
    )


def test_classify_tool_calls_blocks_if_any_tool_is_destructive() -> None:
    tool_by_name = {
        "ado_wit_get_work_item": SimpleNamespace(
            name="ado_wit_get_work_item",
            metadata={"readOnlyHint": True},
        ),
        "databricks_execute_sql": SimpleNamespace(
            name="databricks_execute_sql",
            metadata={},
        ),
    }

    risk, blocked, pending = classify_tool_calls(
        [
            {"name": "ado_wit_get_work_item", "args": {}},
            {"name": "databricks_execute_sql", "args": {"sql": "DROP TABLE prod.users"}},
        ],
        tool_by_name,
    )

    assert risk == "HARD_BLOCK"
    assert blocked == ["databricks_execute_sql"]
    assert pending is None


def test_collect_recent_tool_messages_returns_all_trailing_tool_outputs() -> None:
    messages = [
        HumanMessage(content="Investigate the failure."),
        AIMessage(
            content="",
            tool_calls=[{"name": "ado_wit_get_work_item", "args": {}, "id": "1"}],
        ),
        ToolMessage(content="first result", name="ado_wit_get_work_item", tool_call_id="1"),
        ToolMessage(content="second result", name="ado_wit_get_comments", tool_call_id="2"),
    ]

    recent = collect_recent_tool_messages(messages)

    assert [message.name for message in recent] == [
        "ado_wit_get_work_item",
        "ado_wit_get_comments",
    ]
