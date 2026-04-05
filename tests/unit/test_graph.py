from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agents.graph as graph_module
from agents.graph import (
    approved_tool_batch_matches_last_message,
    classify_tool_call,
    classify_tool_calls,
    collect_recent_tool_messages,
    format_tool_error,
    format_tools_for_gemini,
    max_iterations_for_state,
    terminal_status_for_response,
    traced_tool_call,
)


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


def test_classify_tool_call_treats_generic_learn_intent_as_read() -> None:
    tool = SimpleNamespace(name="azure_keyvault", metadata={})
    assert (
        classify_tool_call(
            tool,
            {"intent": "learn about keyvault commands", "learn": True},
        )
        == "READ"
    )


def test_classify_tool_call_treats_diagnose_command_as_read() -> None:
    tool = SimpleNamespace(name="azure_applens", metadata={})
    assert (
        classify_tool_call(
            tool,
            {
                "command": "diagnose",
                "intent": "diagnose",
                "parameters": '{"resource-type":"ContainerAppJobs"}',
            },
        )
        == "READ"
    )


def test_classify_tool_call_treats_namespaced_diagnose_command_as_read() -> None:
    tool = SimpleNamespace(name="azure_applens", metadata={})
    assert (
        classify_tool_call(
            tool,
            {
                "command": "applens_resource_diagnose",
                "intent": "diagnose issues for Azure Container App Job",
                "parameters": '{"question":"Why is it timing out?"}',
            },
        )
        == "READ"
    )


def test_format_tools_for_gemini_strips_unsupported_schema_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_tool = SimpleNamespace(name="azure_containerapps", metadata={})

    monkeypatch.setattr(
        graph_module,
        "convert_to_openai_tool",
        lambda _tool: {
            "type": "function",
            "function": {
                "name": "azure_containerapps",
                "description": "Inspect Container Apps",
                "parameters": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "command": {
                            "type": "string",
                            "additionalProperties": False,
                        }
                    },
                },
            },
        },
    )

    formatted = format_tools_for_gemini([fake_tool])

    assert formatted == [
        {
            "type": "function",
            "function": {
                "name": "azure_containerapps",
                "description": "Inspect Container Apps",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                        }
                    },
                },
            },
        }
    ]


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

    risk, blocked, pending, pending_batch = classify_tool_calls(
        [
            {"name": "ado_wit_get_work_item", "args": {}},
            {"name": "databricks_execute_sql", "args": {"sql": "DROP TABLE prod.users"}},
        ],
        tool_by_name,
    )

    assert risk == "HARD_BLOCK"
    assert blocked == ["databricks_execute_sql"]
    assert pending is None
    assert pending_batch is None


def test_classify_tool_calls_returns_full_pending_batch_for_approval() -> None:
    tool_by_name = {
        "azure_keyvault": SimpleNamespace(name="azure_keyvault", metadata={}),
        "ado_wit_add_comment": SimpleNamespace(name="ado_wit_add_comment", metadata={}),
    }

    risk, blocked, pending, pending_batch = classify_tool_calls(
        [
            {"name": "azure_keyvault", "args": {"learn": True}},
            {"name": "ado_wit_add_comment", "args": {"work_item_id": 42, "text": "hello"}},
        ],
        tool_by_name,
    )

    assert risk == "WRITE"
    assert blocked == []
    assert pending == {"name": "ado_wit_add_comment", "args": {"work_item_id": 42, "text": "hello"}}
    assert pending_batch == [
        {"name": "azure_keyvault", "args": {"learn": True}},
        {"name": "ado_wit_add_comment", "args": {"work_item_id": 42, "text": "hello"}},
    ]


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


def test_terminal_status_requires_explicit_post_write_completion() -> None:
    state = {
        "write_executed": True,
        "messages": [
            ToolMessage(
                content="Secret rotation completed successfully.",
                name="azure_keyvault_rotate_secret",
                tool_call_id="1",
                status="success",
            )
        ],
    }
    response = AIMessage(content="The write completed successfully and the issue is resolved.")
    assert terminal_status_for_response(state, response) == "RESOLVED"


def test_terminal_status_escalates_without_completed_write() -> None:
    state = {"write_executed": False, "messages": []}
    response = AIMessage(content="I have a diagnosis but have not executed a change yet.")
    assert terminal_status_for_response(state, response) == "ESCALATED"


def test_terminal_status_rejects_uncertain_post_write_conclusion() -> None:
    state = {
        "write_executed": True,
        "messages": [
            ToolMessage(
                content="Action finished.",
                name="azure_resource_restart",
                tool_call_id="1",
                status="success",
            )
        ],
    }
    response = AIMessage(content="The issue seems resolved, but this should verify.")
    assert terminal_status_for_response(state, response) == "INVESTIGATING"


def test_terminal_status_escalates_read_only_diagnosis_without_recovery() -> None:
    state = {
        "write_executed": False,
        "evidence": [{"source": "azure_group_list", "summary": "Located the job resource group."}],
        "messages": [],
    }
    response = AIMessage(content="Most Likely Root Cause: the downstream dependency is timing out.")
    assert terminal_status_for_response(state, response) == "ESCALATED"


def test_terminal_status_resolves_read_only_recovery_confirmation() -> None:
    state = {
        "write_executed": False,
        "evidence": [{"source": "azure_applens", "summary": "The alert has already cleared."}],
        "messages": [],
    }
    response = AIMessage(
        content="The alert has cleared and no further remediation is required."
    )
    assert terminal_status_for_response(state, response) == "RESOLVED"


def test_max_iterations_for_state_uses_azure_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        graph_module,
        "settings",
        SimpleNamespace(
            max_iterations=5,
            azure_max_iterations=8,
            post_write_followup_iterations=2,
        ),
    )

    state = {"target_env": "azure", "write_executed": False}

    assert max_iterations_for_state(state) == 8


def test_max_iterations_for_state_adds_post_write_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        graph_module,
        "settings",
        SimpleNamespace(
            max_iterations=5,
            azure_max_iterations=8,
            post_write_followup_iterations=2,
        ),
    )

    state = {"target_env": "azure", "write_executed": True}

    assert max_iterations_for_state(state) == 10


def test_approved_tool_batch_matches_last_message() -> None:
    state = {
        "pending_tool_calls": [
            {"name": "azure_keyvault", "args": {"learn": True}},
            {"name": "ado_wit_add_comment", "args": {"work_item_id": 42, "text": "hello"}},
        ],
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "azure_keyvault", "args": {"learn": True}, "id": "1"},
                    {
                        "name": "ado_wit_add_comment",
                        "args": {"work_item_id": 42, "text": "hello"},
                        "id": "2",
                    },
                ],
            )
        ],
    }
    assert approved_tool_batch_matches_last_message(state) is True


def test_approved_tool_batch_detects_mismatch() -> None:
    state = {
        "pending_tool_calls": [
            {"name": "ado_wit_add_comment", "args": {"work_item_id": 42, "text": "hello"}},
        ],
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ado_wit_add_comment",
                        "args": {"work_item_id": 42, "text": "changed"},
                        "id": "1",
                    }
                ],
            )
        ],
    }
    assert approved_tool_batch_matches_last_message(state) is False


def test_format_tool_error_normalizes_sensitive_tool_failures() -> None:
    error = RuntimeError(
        "This tool handles sensitive data and requires user consent, but the client "
        "does not support elicitation. Operation rejected for security."
    )
    formatted = format_tool_error(error)
    assert "required interactive user consent" in formatted


@pytest.mark.asyncio
async def test_traced_tool_call_records_tool_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {"statuses": []}

    class _Span:
        pass

    class _Context:
        def __enter__(self):
            return _Span()

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(graph_module, "start_trace_span", lambda *args, **kwargs: _Context())
    monkeypatch.setattr(
        graph_module,
        "set_span_outputs",
        lambda _span, outputs: captured.setdefault("outputs", []).append(outputs),
    )
    monkeypatch.setattr(
        graph_module,
        "set_span_status",
        lambda _span, status: captured["statuses"].append(status),
    )

    request = SimpleNamespace(
        tool_call={"name": "azure_subscription_list", "id": "tool-1", "args": {}},
        state={"target_env": "azure"},
    )

    async def execute(_request):
        return ToolMessage(
            content='{"subscriptions":["sub-a"]}',
            name="azure_subscription_list",
            tool_call_id="tool-1",
            status="success",
        )

    result = await traced_tool_call(request, execute)

    assert isinstance(result, ToolMessage)
    assert captured["outputs"] == [
        {
            "tool_name": "azure_subscription_list",
            "name": "azure_subscription_list",
            "tool_call_id": "tool-1",
            "status": "success",
            "content": '{"subscriptions":["sub-a"]}',
        }
    ]
    assert captured["statuses"] == []


@pytest.mark.asyncio
async def test_traced_tool_call_marks_error_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses: list[str] = []

    class _Span:
        pass

    class _Context:
        def __enter__(self):
            return _Span()

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(graph_module, "start_trace_span", lambda *args, **kwargs: _Context())
    monkeypatch.setattr(graph_module, "set_span_outputs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        graph_module,
        "set_span_status",
        lambda _span, status: statuses.append(status),
    )

    request = SimpleNamespace(
        tool_call={"name": "azure_applens", "id": "tool-2", "args": {}},
        state={"target_env": "azure"},
    )

    async def execute(_request):
        return ToolMessage(
            content="Consent required.",
            name="azure_applens",
            tool_call_id="tool-2",
            status="error",
        )

    await traced_tool_call(request, execute)

    assert statuses == ["ERROR"]
