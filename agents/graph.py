"""
Agent orchestration layer.

Uses a custom StateGraph rather than LangGraph's default ReAct helper so we can:
  - handle Gemini malformed function calls explicitly
  - gate risky MCP tools behind a guardrail node
  - capture evidence from tool observations in structured state
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypedDict

import structlog
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from config import settings
from db import RunStatus
from observability import (
    set_span_attributes,
    set_span_outputs,
    set_span_status,
    start_trace_span,
)
from prompts import registry

log = structlog.get_logger()


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]

    run_id: str
    work_item_id: int
    thread_id: str
    attempt_count: int

    failure_type: str
    target_env: str
    hypothesis: str
    evidence: list[dict]

    iteration_count: int
    status: str
    retries: int

    proposed_fix: dict | None
    pending_tool_call: dict | None
    pending_tool_calls: list[dict] | None
    blocked_tools: list[str]
    write_executed: bool


HARD_BLOCK_PATTERNS = [
    r"DROP\s+TABLE",
    r"TRUNCATE\s+TABLE",
    r"DELETE\s+FROM",
    r"ALTER\s+TABLE.+DROP",
    r"az\s+group\s+delete",
    r"rm\s+-rf",
    r"kubectl\s+delete",
    r"databricks\s+clusters\s+delete",
]

READ_NAME_HINTS = (
    "learn",
    "help",
    "get",
    "list",
    "read",
    "search",
    "fetch",
    "lookup",
    "show",
    "describe",
    "inspect",
)

WRITE_NAME_HINTS = (
    "create",
    "update",
    "delete",
    "merge",
    "restart",
    "repair",
    "post",
    "comment",
    "write",
    "trigger",
    "execute",
    "run",
    "cancel",
    "approve",
)

READ_SQL_PREFIX = re.compile(
    r"^\s*(select\b|show\b|describe\b|explain\b|with\b.+\bselect\b|values\b)",
    re.IGNORECASE | re.DOTALL,
)

READ_ARG_HINTS = (
    "learn",
    "help",
    "doc",
    "docs",
    "documentation",
    "capabilities",
    "available commands",
    "what commands",
    "list",
    "show",
    "describe",
    "inspect",
    "read",
    "lookup",
    "fetch",
    "status",
)

READ_COMMAND_HINTS = (
    "diagnose",
    "analyze",
    "analyse",
    "investigate",
    "triage",
    "health",
    "healthcheck",
    "status",
)

WRITE_ARG_HINTS = (
    "create",
    "update",
    "delete",
    "remove",
    "restart",
    "repair",
    "deploy",
    "execute",
    "run",
    "cancel",
    "approve",
    "grant",
    "revoke",
    "rotate",
    "set",
    "enable",
    "disable",
)

WRITE_COMMAND_HINTS = (
    "create",
    "update",
    "delete",
    "remove",
    "restart",
    "repair",
    "deploy",
    "execute",
    "run",
    "cancel",
    "approve",
    "grant",
    "revoke",
    "rotate",
    "set",
    "enable",
    "disable",
)

RESOLUTION_HINTS = (
    "resolved",
    "resolution",
    "fixed",
    "successful",
    "successfully",
    "completed",
    "applied",
    "granted",
    "updated",
    "restarted",
)

RECOVERY_HINTS = (
    "already recovered",
    "has recovered",
    "already healthy",
    "back to healthy",
    "issue has cleared",
    "alert has cleared",
    "already resolved",
    "service is healthy again",
    "no further remediation is required",
    "no remediation is required",
    "no action is required because the issue is already resolved",
)

UNCERTAINTY_HINTS = (
    "maybe",
    "might",
    "likely",
    "possibly",
    "appears",
    "seems",
    "unclear",
    "not sure",
    "cannot confirm",
    "should verify",
)


def _tool_metadata(tool: BaseTool | None) -> dict:
    if tool is None or tool.metadata is None:
        return {}
    return dict(tool.metadata)


def _has_hint(text: str, hints: tuple[str, ...]) -> bool:
    return any(
        re.search(rf"\b{re.escape(hint)}\b", text, re.IGNORECASE) is not None
        for hint in hints
    )


def _tool_args_text(tool_args: dict) -> str:
    if not tool_args:
        return ""
    parts: list[str] = []
    ignored_keys = {"parameters", "question", "prompt", "body", "message", "text", "notes"}
    for key, value in tool_args.items():
        if str(key).lower() in ignored_keys:
            continue
        if isinstance(value, bool):
            if value:
                parts.append(str(key))
            continue
        if isinstance(value, (str, int, float)):
            parts.append(f"{key} {value}")
            continue
        parts.append(f"{key} {json.dumps(value, sort_keys=True)}")
    return " ".join(parts).lower()


def _tool_command_values(tool_args: dict) -> list[str]:
    command_values: list[str] = []
    for key in ("command", "intent", "action", "operation", "verb", "mode"):
        value = tool_args.get(key)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized:
                command_values.append(normalized)
    return command_values


def _tokenize_command_values(tool_args: dict) -> list[str]:
    tokens: list[str] = []
    for value in _tool_command_values(tool_args):
        tokens.extend(token for token in re.split(r"[^a-z0-9]+", value) if token)
    return tokens


def _normalized_tool_batch(tool_calls: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for tool_call in tool_calls:
        normalized.append(
            {
                "name": str(tool_call.get("name", "")),
                "args": dict(tool_call.get("args", {}) or {}),
            }
        )
    return normalized


UNSUPPORTED_GEMINI_SCHEMA_KEYS = {"$schema", "additionalProperties"}


def _sanitize_gemini_schema(value):
    if isinstance(value, dict):
        cleaned: dict = {}
        for key, nested in value.items():
            if key in UNSUPPORTED_GEMINI_SCHEMA_KEYS:
                continue
            cleaned[key] = _sanitize_gemini_schema(nested)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_gemini_schema(item) for item in value]
    return value


def format_tools_for_gemini(tools: list[BaseTool]) -> list[dict]:
    formatted: list[dict] = []
    for tool in tools:
        tool_schema = convert_to_openai_tool(tool)
        formatted.append(_sanitize_gemini_schema(tool_schema))
    return formatted


def _message_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
        return " ".join(parts).strip()
    return str(content).strip()


def _tool_call_summary(tool_calls: list[dict]) -> list[dict]:
    return [
        {
            "name": tool_call.get("name", ""),
            "args": _normalized_tool_batch([tool_call])[0]["args"],
        }
        for tool_call in tool_calls[:10]
    ]


def _recent_tool_messages_confirm_success(messages: list[BaseMessage]) -> bool:
    tool_messages = collect_recent_tool_messages(messages)
    if not tool_messages:
        return False

    for message in tool_messages:
        status = (message.status or "success").lower()
        if status not in {"success", "ok"}:
            return False
        text = _message_text(message.content).lower()
        if (
            "tool execution failed" in text
            or "requires user consent" in text
            or "operation rejected" in text
        ):
            return False
    return True


def _response_confidently_resolves(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in RESOLUTION_HINTS) and not any(
        hint in lowered for hint in UNCERTAINTY_HINTS
    )


def _response_confirms_recovery(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in RECOVERY_HINTS) and not any(
        hint in lowered for hint in UNCERTAINTY_HINTS
    )


def terminal_status_for_response(state: AgentState, response: AIMessage) -> str:
    if getattr(response, "tool_calls", None):
        return RunStatus.INVESTIGATING.value

    text = _message_text(response.content)
    if not text:
        if state.get("write_executed", False):
            return RunStatus.INVESTIGATING.value
        return RunStatus.ESCALATED.value

    lowered = text.lower()
    if lowered.startswith("escalate:"):
        return RunStatus.ESCALATED.value

    if not state.get("write_executed", False):
        if state.get("evidence") and _response_confirms_recovery(text):
            return RunStatus.RESOLVED.value
        return RunStatus.ESCALATED.value

    if _recent_tool_messages_confirm_success(state["messages"]) and _response_confidently_resolves(
        text
    ):
        return RunStatus.RESOLVED.value

    return RunStatus.INVESTIGATING.value


def approved_tool_batch_matches_last_message(state: AgentState) -> bool:
    expected = _normalized_tool_batch(state.get("pending_tool_calls") or [])
    if not expected:
        return False

    last = state["messages"][-1]
    actual = _normalized_tool_batch(list(getattr(last, "tool_calls", []) or []))
    return actual == expected


def format_tool_error(exc: Exception) -> str:
    text = str(exc).strip()
    lowered = text.lower()
    if "requires user consent" in lowered or "elicitation" in lowered:
        return (
            "Tool execution failed because the MCP server required interactive user consent, "
            "which this worker cannot provide. Treat this as a blocked sensitive operation."
        )
    return f"Tool execution failed: {text}"


def _tool_trace_result_payload(result: ToolMessage | Command) -> dict[str, Any]:
    if isinstance(result, ToolMessage):
        return {
            "name": result.name,
            "tool_call_id": result.tool_call_id,
            "status": result.status or "success",
            "content": result.content,
        }
    return {"command": repr(result)}


async def traced_tool_call(
    request: ToolCallRequest,
    execute: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
) -> ToolMessage | Command:
    tool_call = request.tool_call
    with start_trace_span(
        "graph.tool",
        span_type="TOOL",
        attributes={
            "tool_name": tool_call["name"],
            "tool_call_id": tool_call.get("id", ""),
            "target_env": getattr(request, "state", {}).get("target_env", "unknown")
            if isinstance(getattr(request, "state", None), dict)
            else "unknown",
        },
        inputs={
            "tool_name": tool_call["name"],
            "tool_call_id": tool_call.get("id", ""),
            "args": tool_call.get("args", {}),
        },
    ) as span:
        try:
            result = await execute(request)
        except Exception as exc:
            set_span_status(span, "ERROR")
            set_span_outputs(
                span,
                {
                    "tool_name": tool_call["name"],
                    "status": "error",
                    "error": format_tool_error(exc),
                },
            )
            raise

        result_payload = _tool_trace_result_payload(result)
        status = str(result_payload.get("status", "success")).lower()
        if status not in {"success", "ok"}:
            set_span_status(span, "ERROR")
        set_span_outputs(
            span,
            {
                "tool_name": tool_call["name"],
                **result_payload,
            },
        )
        return result


def classify_tool_call(tool: BaseTool | None, tool_args: dict) -> str:
    """
    Classify the pending tool invocation.

    Returns one of READ, WRITE, or HARD_BLOCK.
    """
    metadata = _tool_metadata(tool)
    if metadata.get("destructiveHint") is True:
        return "HARD_BLOCK"
    if metadata.get("readOnlyHint") is True:
        return "READ"

    tool_name = tool.name.lower() if tool else ""
    args_text = _tool_args_text(tool_args)
    command_tokens = _tokenize_command_values(tool_args)
    if tool_args.get("learn") is True or tool_args.get("help") is True:
        return "READ"

    sql = str(tool_args.get("query") or tool_args.get("sql") or "")
    for pattern in HARD_BLOCK_PATTERNS:
        if re.search(pattern, sql, re.IGNORECASE):
            return "HARD_BLOCK"

    if sql:
        if READ_SQL_PREFIX.match(sql):
            return "READ"
        return "WRITE"

    if tool_name == "azure_applens" and command_tokens:
        if any(command in READ_COMMAND_HINTS for command in command_tokens):
            return "READ"

    if command_tokens:
        if any(command in WRITE_COMMAND_HINTS for command in command_tokens):
            return "WRITE"
        if any(command in READ_COMMAND_HINTS for command in command_tokens):
            return "READ"

    if args_text:
        if _has_hint(args_text, WRITE_ARG_HINTS):
            return "WRITE"
        if _has_hint(args_text, READ_ARG_HINTS):
            return "READ"

    if any(hint in tool_name for hint in WRITE_NAME_HINTS):
        return "WRITE"
    if any(hint in tool_name for hint in READ_NAME_HINTS):
        return "READ"
    return "WRITE"


def max_iterations_for_state(state: AgentState) -> int:
    target_env = (state.get("target_env") or "").lower()
    limit = settings.max_iterations
    if target_env == "azure":
        limit = max(limit, settings.azure_max_iterations)
    if state.get("write_executed", False):
        limit += settings.post_write_followup_iterations
    return limit


def classify_tool_calls(
    tool_calls: list[dict],
    tool_by_name: dict[str, BaseTool],
) -> tuple[str, list[str], dict | None, list[dict] | None]:
    """
    Assess a batch of pending tool calls.

    Returns:
      - overall risk: READ, WRITE, or HARD_BLOCK
      - blocked tool names
      - the first pending write tool call, when present
      - the full pending tool batch, when approval is required
    """
    blocked_tools: list[str] = []
    pending_write: dict | None = None

    for tool_call in tool_calls:
        tool_name = tool_call["name"]
        tool_args = tool_call.get("args", {})
        risk = classify_tool_call(tool_by_name.get(tool_name), tool_args)
        log.info("agent.guardrail", tool=tool_name, risk=risk)
        if risk == "HARD_BLOCK":
            blocked_tools.append(tool_name)
            continue
        if risk == "WRITE" and pending_write is None:
            pending_write = {"name": tool_name, "args": tool_args}

    if blocked_tools:
        return "HARD_BLOCK", blocked_tools, None, None
    if pending_write:
        return "WRITE", [], pending_write, _normalized_tool_batch(tool_calls)
    return "READ", [], None, None


def collect_recent_tool_messages(messages: list[BaseMessage]) -> list[ToolMessage]:
    recent: list[ToolMessage] = []
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            break
        recent.append(message)
    return list(reversed(recent))


def make_llm(tools: list[BaseTool] | None = None) -> ChatGoogleGenerativeAI:
    llm = ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.google_api_key,
        temperature=settings.gemini_temperature,
        max_retries=2,
    )
    if not tools:
        return llm
    return llm.bind(tools=format_tools_for_gemini(tools))


def build_graph(tools: list[BaseTool]) -> StateGraph:
    llm = make_llm(tools)
    tool_by_name = {tool.name: tool for tool in tools}

    async def reason(state: AgentState) -> dict:
        with start_trace_span(
            "graph.reason",
            span_type="LLM",
            attributes={
                "iteration": state["iteration_count"],
                "attempt": state["attempt_count"],
                "target_env": state.get("target_env", "unknown"),
                "message_count": len(state["messages"]),
            },
            inputs={
                "iteration": state["iteration_count"],
                "attempt": state["attempt_count"],
                "status": state.get("status", ""),
            },
        ) as span:
            log.info(
                "agent.reason",
                iteration=state["iteration_count"],
                attempt=state["attempt_count"],
            )

            iteration_limit = max_iterations_for_state(state)
            system_prompt = registry.get("system")
            rendered_system_prompt = system_prompt.render(
                max_iter=iteration_limit,
                target_env=state.get("target_env", "unknown"),
                attempt=state["attempt_count"] + 1,
                max_attempts=settings.max_attempts,
            )
            log.info(
                "agent.prompt_version",
                prompt=system_prompt.fingerprint,
                iteration=state["iteration_count"],
            )
            set_span_attributes(
                span,
                {
                    "system_prompt": system_prompt.fingerprint,
                    "model": settings.gemini_model,
                },
            )
            system = SystemMessage(content=rendered_system_prompt)
            msgs = [system] + list(state["messages"])

            try:
                response = await llm.ainvoke(msgs)
            except Exception as exc:
                log.error("agent.llm_error", error=str(exc))
                set_span_status(span, "ERROR")
                set_span_outputs(span, {"status": RunStatus.ESCALATED.value, "error": str(exc)})
                return {
                    "messages": [AIMessage(content=f"LLM error: {exc}")],
                    "status": RunStatus.ESCALATED.value,
                }

            finish_reason = getattr(response, "response_metadata", {}).get("finish_reason", "")
            tool_call_summary = _tool_call_summary(list(getattr(response, "tool_calls", []) or []))
            set_span_outputs(
                span,
                {
                    "finish_reason": finish_reason,
                    "tool_calls": tool_call_summary,
                    "response_preview": _message_text(response.content),
                },
            )
            if finish_reason == "MALFORMED_FUNCTION_CALL":
                retries = state.get("retries", 0)
                log.warning("agent.malformed_tool_call", retries=retries)
                set_span_status(span, "ERROR")
                if retries < 3:
                    corrective_prompt = registry.get("tool_retry")
                    log.info(
                        "agent.prompt_version",
                        prompt=corrective_prompt.fingerprint,
                        iteration=state["iteration_count"],
                    )
                    corrective = HumanMessage(content=corrective_prompt.render())
                    return {
                        "messages": [response, corrective],
                        "retries": retries + 1,
                        "status": RunStatus.INVESTIGATING.value,
                    }
                return {
                    "messages": [AIMessage(content="Repeated malformed tool calls. Escalating.")],
                    "status": RunStatus.ESCALATED.value,
                }

            next_iteration = state["iteration_count"] + 1
            if next_iteration >= iteration_limit and getattr(response, "tool_calls", None):
                overall_risk, _, _, _ = classify_tool_calls(
                    list(getattr(response, "tool_calls", []) or []),
                    tool_by_name,
                )
                if overall_risk == "READ" and (state.get("target_env") or "").lower() == "azure":
                    summary_prompt = registry.get("tool_budget_summary")
                    log.info(
                        "agent.prompt_version",
                        prompt=summary_prompt.fingerprint,
                        iteration=state["iteration_count"],
                    )
                    final_summary_request = HumanMessage(
                        content=summary_prompt.render(
                            target_env=state.get("target_env", "unknown"),
                        )
                    )
                    final_response = await llm.ainvoke(msgs + [response, final_summary_request])
                    if getattr(final_response, "tool_calls", None):
                        set_span_status(span, "ERROR")
                        return {
                            "messages": [
                                AIMessage(
                                    content=(
                                        "I exhausted the read-only investigation budget and still "
                                        "attempted to call more tools. Escalating to a human "
                                        "engineer."
                                    )
                                )
                            ],
                            "iteration_count": next_iteration,
                            "retries": 0,
                            "status": RunStatus.ESCALATED.value,
                        }

                    result = {
                        "messages": [response, final_summary_request, final_response],
                        "iteration_count": next_iteration,
                        "retries": 0,
                        "status": terminal_status_for_response(state, final_response),
                    }
                    set_span_outputs(
                        span,
                        {
                            "finish_reason": finish_reason,
                            "tool_calls": tool_call_summary,
                            "response_preview": _message_text(final_response.content),
                            "status": result["status"],
                            "forced_text_summary": True,
                            "budget_prompt": summary_prompt.fingerprint,
                        },
                    )
                    return result

                set_span_status(span, "ERROR")
                return {
                    "messages": [
                        AIMessage(
                            content=(
                                "I reached the investigation step limit before reaching a safe "
                                "diagnosis. Escalating to a human engineer."
                            )
                        )
                    ],
                    "iteration_count": next_iteration,
                    "retries": 0,
                    "status": RunStatus.ESCALATED.value,
                }

            result = {
                "messages": [response],
                "iteration_count": next_iteration,
                "retries": 0,
                "status": terminal_status_for_response(state, response),
            }
            set_span_outputs(
                span,
                {
                    "finish_reason": finish_reason,
                    "tool_calls": tool_call_summary,
                    "response_preview": _message_text(response.content),
                    "status": result["status"],
                },
            )
            return result

    async def guardrail(state: AgentState) -> dict:
        last = state["messages"][-1]
        tool_calls = list(getattr(last, "tool_calls", []) or [])
        if not tool_calls:
            return {}

        with start_trace_span(
            "graph.guardrail",
            span_type="GUARDRAIL",
            inputs={"tool_calls": _tool_call_summary(tool_calls)},
        ) as span:
            overall_risk, blocked_tools, pending_write, pending_tool_calls = classify_tool_calls(
                tool_calls, tool_by_name
            )
            set_span_outputs(
                span,
                {
                    "overall_risk": overall_risk,
                    "blocked_tools": blocked_tools,
                    "pending_write": pending_write,
                },
            )
            if overall_risk == "HARD_BLOCK":
                set_span_status(span, "ERROR")
                blocked = list(state.get("blocked_tools", [])) + blocked_tools
                blocked_display = "`, `".join(blocked_tools)
                return {
                    "status": RunStatus.ESCALATED.value,
                    "blocked_tools": blocked,
                    "messages": [
                        HumanMessage(
                        content=(
                            "GUARDRAIL: Tool call(s) "
                            f"`{blocked_display}` were blocked because they appear "
                            "destructive. "
                            "Escalating to a human engineer."
                        )
                    )
                    ],
                }
            if overall_risk == "WRITE" and pending_write is not None:
                return {
                    "status": RunStatus.AWAITING_APPROVAL.value,
                    "pending_tool_call": pending_write,
                    "pending_tool_calls": pending_tool_calls,
                }

            return {}

    async def execute_write(state: AgentState) -> dict:
        with start_trace_span(
            "graph.execute_write",
            span_type="GUARDRAIL",
            inputs={"pending_tool_calls": state.get("pending_tool_calls") or []},
        ) as span:
            if not approved_tool_batch_matches_last_message(state):
                set_span_status(span, "ERROR")
                set_span_outputs(span, {"status": RunStatus.ESCALATED.value, "matched": False})
                return {
                    "status": RunStatus.ESCALATED.value,
                    "pending_tool_call": None,
                    "pending_tool_calls": None,
                    "messages": [
                        AIMessage(
                            content=(
                                "ESCALATE: The approved tool batch no longer matches the exact "
                                "tool calls captured for approval. Refusing to execute an "
                                "unverified write batch."
                            )
                        )
                    ],
                }
            result = {
                "status": RunStatus.INVESTIGATING.value,
                "pending_tool_call": None,
                "pending_tool_calls": None,
                "write_executed": True,
            }
            set_span_outputs(span, {"status": result["status"], "matched": True})
            return result

    async def record_evidence(state: AgentState) -> dict:
        with start_trace_span(
            "graph.record_evidence",
            span_type="TASK",
            attributes={"message_count": len(state["messages"])},
        ) as span:
            tool_messages = collect_recent_tool_messages(state["messages"])
            if not tool_messages:
                set_span_outputs(span, {"evidence_added": 0})
                return {}

            evidence = list(state.get("evidence", []))
            for message in tool_messages:
                evidence.append(
                    {
                        "source": message.name or "tool",
                        "summary": _summarize_tool_message(message),
                        "status": message.status,
                        "tool_call_id": message.tool_call_id,
                    }
                )
            set_span_outputs(
                span,
                {
                    "evidence_added": len(tool_messages),
                    "sources": [message.name or "tool" for message in tool_messages],
                },
            )
            return {"evidence": evidence, "status": RunStatus.INVESTIGATING.value}

    def route_after_reason(state: AgentState) -> str:
        status = state.get("status")
        if status in (RunStatus.ESCALATED.value, RunStatus.AWAITING_APPROVAL.value):
            return "terminal"
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "guardrail"
        return "terminal"

    def route_after_guardrail(state: AgentState) -> str:
        status = state.get("status")
        if status == RunStatus.ESCALATED.value:
            return "terminal"
        if status == RunStatus.AWAITING_APPROVAL.value:
            return "execute_write"
        return "tools"

    def route_after_execute_write(state: AgentState) -> str:
        if state.get("status") == RunStatus.ESCALATED.value:
            return "terminal"
        return "tools"

    tool_node = ToolNode(
        tools,
        handle_tool_errors=format_tool_error,
        awrap_tool_call=traced_tool_call,
    )

    graph = StateGraph(AgentState)
    graph.add_node("reason", reason)
    graph.add_node("guardrail", guardrail)
    graph.add_node("tools", tool_node)
    graph.add_node("record_evidence", record_evidence)
    graph.add_node("execute_write", execute_write)

    graph.add_edge(START, "reason")
    graph.add_conditional_edges(
        "reason",
        route_after_reason,
        {
            "guardrail": "guardrail",
            "terminal": END,
        },
    )
    graph.add_conditional_edges(
        "guardrail",
        route_after_guardrail,
        {
            "tools": "tools",
            "execute_write": "execute_write",
            "terminal": END,
        },
    )
    graph.add_edge("tools", "record_evidence")
    graph.add_edge("record_evidence", "reason")
    graph.add_conditional_edges(
        "execute_write",
        route_after_execute_write,
        {
            "tools": "tools",
            "terminal": END,
        },
    )

    return graph


def _summarize_tool_message(message: ToolMessage, limit: int = 280) -> str:
    content = message.content
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        text = " ".join(parts)
    else:
        text = str(content)

    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."
