"""
Worker entry point for Azure-hosted execution.

This worker supports two execution modes:
  - direct mode, where JOB_PAYLOAD contains a single run envelope
  - queue mode, where it pulls one run envelope from Azure Service Bus
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Literal

import structlog
from langchain_core.messages import AIMessage, HumanMessage

from agents.graph import AgentState, build_graph
from config import settings
from db import RunStatus, bootstrap, ensure_run, get_checkpointer, get_run, update_status
from integrations.ado import AdoClient, AdoWorkflowStage, ado_state_for_stage
from mcp_client import load_mcp_tools
from observability import (
    set_span_attributes,
    set_span_inputs,
    set_span_outputs,
    set_span_status,
    start_trace_span,
    update_trace,
)
from prompts import build_approval_context_message, build_investigation_prompt, registry
from queueing import normalize_run_message, process_next_run_message

log = structlog.get_logger()

WorkerAction = Literal["investigate", "resume"]


async def run_agent(work_item_id: int, *, action: WorkerAction = "investigate") -> None:
    with start_trace_span(
        "worker.run_agent",
        span_type="AGENT",
        attributes={"work_item_id": work_item_id, "action": action},
        inputs={"work_item_id": work_item_id, "action": action},
    ) as trace_span:
        update_trace(
            tags={"component": "worker", "action": action},
            metadata={"work_item_id": work_item_id},
            client_request_id=f"work-item-{work_item_id}-{action}",
            request_preview=f"work_item_id={work_item_id} action={action}",
        )

        def _finish_trace(
            status: str,
            *,
            conclusion: str = "",
            blocked_tools: list[str] | None = None,
            pending_tool_call: dict | None = None,
            attempt_count: int | None = None,
        ) -> None:
            final_state = "ERROR" if status == RunStatus.ESCALATED.value else "OK"
            set_span_status(trace_span, final_state)
            set_span_outputs(
                trace_span,
                {
                    "status": status,
                    "attempt_count": attempt_count,
                    "blocked_tools": blocked_tools or [],
                    "pending_tool_call": pending_tool_call or {},
                    "conclusion": conclusion,
                },
            )
            update_trace(
                tags={"final_status": status},
                response_preview=conclusion or status,
                state=final_state,
            )

        run = await get_run(work_item_id)
        if not run:
            log.error("worker.run_not_found", work_item_id=work_item_id)
            _finish_trace("RUN_NOT_FOUND", conclusion="Run record not found.")
            return

        thread_id = run["thread_id"]
        attempt = run["attempt_count"]
        set_span_attributes(trace_span, {"thread_id": thread_id, "attempt": attempt})
        update_trace(
            metadata={"thread_id": thread_id, "attempt": attempt},
            client_request_id=f"work-item-{work_item_id}-{action}-attempt-{attempt}",
        )
        log.info(
            "worker.start",
            work_item_id=work_item_id,
            thread_id=thread_id,
            attempt=attempt,
            action=action,
        )

        if action != "resume" and attempt >= settings.max_attempts:
            log.warning("worker.max_attempts_reached", work_item_id=work_item_id)
            await update_status(work_item_id, RunStatus.ESCALATED)
            async with AdoClient() as ado:
                with start_trace_span(
                    "worker.publish_max_attempts_escalation",
                    span_type="TASK",
                    inputs={"work_item_id": work_item_id, "attempt": attempt},
                ) as span:
                    await ado.add_comment(
                        work_item_id,
                        _format_escalation(
                            "The agent reached the configured maximum number of attempts "
                            "before a new investigation could begin.",
                            [],
                            attempt,
                        ),
                    )
                    await _sync_ado_state(ado, work_item_id, "escalated")
                    set_span_outputs(span, {"status": RunStatus.ESCALATED.value})
            _finish_trace(
                RunStatus.ESCALATED.value,
                conclusion="Maximum attempts reached before investigation started.",
                attempt_count=attempt,
            )
            return

        async with AdoClient() as ado:
            with start_trace_span(
                "worker.fetch_context",
                span_type="TASK",
                inputs={"work_item_id": work_item_id, "action": action},
            ) as span:
                work_item = await ado.get_work_item(work_item_id)
                comments = await ado.get_comments(work_item_id)
                run = await ensure_run(
                    work_item_id=work_item_id,
                    work_item_url=work_item.get("url", ""),
                    target_env=work_item.get("target_env", "databricks"),
                    failure_type=work_item.get("failure_type", "unknown"),
                )
                thread_id = run["thread_id"]
                attempt = run["attempt_count"]
                target_env = run.get("target_env") or work_item.get("target_env", "databricks")
                failure_type = run.get("failure_type") or work_item.get("failure_type", "unknown")
                set_span_outputs(
                    span,
                    {
                        "thread_id": thread_id,
                        "attempt": attempt,
                        "target_env": target_env,
                        "failure_type": failure_type,
                        "incident_target": work_item.get("incident_target", ""),
                        "comment_count": len(comments),
                    },
                )

            set_span_attributes(
                trace_span,
                {
                    "thread_id": thread_id,
                    "attempt": attempt,
                    "target_env": target_env,
                    "failure_type": failure_type,
                    "incident_target": work_item.get("incident_target", ""),
                },
            )
            update_trace(
                metadata={
                    "thread_id": thread_id,
                    "attempt": attempt,
                    "target_env": target_env,
                    "failure_type": failure_type,
                    "incident_target": work_item.get("incident_target", ""),
                },
                request_preview=(
                    f"work_item_id={work_item_id} action={action} target_env={target_env} "
                    "failure_type="
                    f"{failure_type} incident_target={work_item.get('incident_target', '')}"
                ),
            )
            set_span_inputs(
                trace_span,
                {
                    "work_item_id": work_item_id,
                    "action": action,
                    "thread_id": thread_id,
                    "attempt": attempt,
                    "target_env": target_env,
                    "failure_type": failure_type,
                    "incident_target": work_item.get("incident_target", ""),
                    "alert_rule": work_item.get("alert_rule", ""),
                    "target_resource_id": work_item.get("target_resource_id", ""),
                    "target_resource_name": work_item.get("target_resource_name", ""),
                    "target_resource_group": work_item.get("target_resource_group", ""),
                    "target_resource_type": work_item.get("target_resource_type", ""),
                    "target_subscription": work_item.get("target_subscription", ""),
                    "work_item_title": work_item.get("fields", {}).get("System.Title", ""),
                    "work_item_url": work_item.get("url", ""),
                    "comment_count": len(comments),
                },
            )

            await update_status(work_item_id, RunStatus.INVESTIGATING)
            await _sync_ado_state(ado, work_item_id, "investigating")
            try:
                with start_trace_span(
                    "worker.load_tools",
                    span_type="TASK",
                    inputs={"target_env": target_env},
                ) as span:
                    tools = await load_mcp_tools(target_env=target_env)
                    set_span_outputs(
                        span,
                        {
                            "tool_count": len(tools),
                            "tool_names": [tool.name for tool in tools[:20]],
                        },
                    )
            except Exception as exc:
                log.error(
                    "worker.tool_load_failed",
                    work_item_id=work_item_id,
                    target_env=target_env,
                    error=str(exc),
                )
                await ado.add_comment(
                    work_item_id,
                    _format_escalation(
                        "The worker could not load the required MCP tools for this run. "
                        f"Configuration error: {exc}",
                        [],
                        attempt,
                    ),
                )
                await update_status(work_item_id, RunStatus.ESCALATED)
                await _sync_ado_state(ado, work_item_id, "escalated")
                _finish_trace(
                    RunStatus.ESCALATED.value,
                    conclusion=f"MCP tool load failed: {exc}",
                    attempt_count=attempt,
                )
                return
            graph_def = build_graph(tools)

            try:
                with start_trace_span(
                    "worker.graph_invoke",
                    span_type="WORKFLOW",
                    inputs={
                        "thread_id": thread_id,
                        "action": action,
                        "attempt": attempt,
                        "target_env": target_env,
                    },
                ) as span:
                    async with get_checkpointer() as checkpointer:
                        await bootstrap(checkpointer)
                        compiled = graph_def.compile(
                            checkpointer=checkpointer,
                            interrupt_before=["execute_write"],
                        )
                        config = {"configurable": {"thread_id": thread_id}}

                        if action == "resume":
                            snapshot = await compiled.aget_state(config)
                            set_span_attributes(
                                span,
                                {"pending_checkpoint_nodes": list(snapshot.next or [])},
                            )
                            if not snapshot.next:
                                log.warning(
                                    "worker.no_pending_checkpoint",
                                    work_item_id=work_item_id,
                                )
                                await ado.add_comment(
                                    work_item_id,
                                    "No pending approval step was found to resume. The saved "
                                    "checkpoint and Neon run state are inconsistent, so the "
                                    "incident is being escalated for human review.",
                                )
                                await update_status(work_item_id, RunStatus.ESCALATED)
                                await _sync_ado_state(ado, work_item_id, "escalated")
                                set_span_status(span, "ERROR")
                                set_span_outputs(
                                    span,
                                    {
                                        "status": RunStatus.ESCALATED.value,
                                        "reason": "no_pending_checkpoint",
                                    },
                                )
                                _finish_trace(
                                    RunStatus.ESCALATED.value,
                                    conclusion=(
                                        "No pending approval checkpoint was available for resume."
                                    ),
                                    attempt_count=attempt,
                                )
                                return
                            final_state = await compiled.ainvoke(None, config)
                        else:
                            investigation_prompt = registry.get("investigation")
                            initial_message = build_investigation_prompt(
                                work_item=work_item,
                                comments=comments,
                                attempt=attempt,
                                prompt=investigation_prompt,
                            )
                            prompt_versions = {
                                "investigation": investigation_prompt.fingerprint,
                                "system": registry.get("system").fingerprint,
                                "tool_retry": registry.get("tool_retry").fingerprint,
                            }
                            feedback_message = _latest_retry_feedback_message(comments)
                            if feedback_message:
                                prompt_versions["approval_feedback"] = registry.get(
                                    "approval_feedback"
                                ).fingerprint
                            log.info(
                                "worker.prompt_versions",
                                work_item_id=work_item_id,
                                prompts=prompt_versions,
                            )
                            set_span_attributes(span, {"prompt_versions": prompt_versions})
                            messages = [HumanMessage(content=initial_message)]
                            if feedback_message:
                                messages.append(HumanMessage(content=feedback_message))
                            set_span_inputs(
                                trace_span,
                                {
                                    "work_item_id": work_item_id,
                                    "action": action,
                                    "thread_id": thread_id,
                                    "attempt": attempt,
                                    "target_env": target_env,
                                    "failure_type": failure_type,
                                    "incident_target": work_item.get("incident_target", ""),
                                    "alert_rule": work_item.get("alert_rule", ""),
                                    "target_resource_id": work_item.get(
                                        "target_resource_id", ""
                                    ),
                                    "target_resource_name": work_item.get(
                                        "target_resource_name", ""
                                    ),
                                    "target_resource_group": work_item.get(
                                        "target_resource_group", ""
                                    ),
                                    "target_resource_type": work_item.get(
                                        "target_resource_type", ""
                                    ),
                                    "target_subscription": work_item.get(
                                        "target_subscription", ""
                                    ),
                                    "work_item_title": work_item.get("fields", {}).get(
                                        "System.Title", ""
                                    ),
                                    "investigation_prompt_preview": initial_message,
                                    "latest_retry_feedback": feedback_message or "",
                                },
                            )
                            initial_state: AgentState = {
                                "messages": messages,
                                "run_id": str(run["run_id"]),
                                "work_item_id": work_item_id,
                                "thread_id": thread_id,
                                "attempt_count": attempt,
                                "failure_type": failure_type,
                                "target_env": target_env,
                                "hypothesis": "",
                                "evidence": [],
                                "iteration_count": 0,
                                "status": RunStatus.INVESTIGATING.value,
                                "retries": 0,
                                "proposed_fix": None,
                                "pending_tool_call": None,
                                "pending_tool_calls": None,
                                "blocked_tools": [],
                                "write_executed": False,
                            }
                            final_state = await compiled.ainvoke(initial_state, config)
                        set_span_outputs(
                            span,
                            {
                                "status": final_state.get("status", ""),
                                "blocked_tools": final_state.get("blocked_tools", []),
                                "evidence_count": len(final_state.get("evidence", [])),
                            },
                        )
            except Exception as exc:
                log.exception(
                    "worker.graph_execution_failed",
                    work_item_id=work_item_id,
                    action=action,
                    error=str(exc),
                )
                await ado.add_comment(
                    work_item_id,
                    _format_escalation(
                        "The agent run failed unexpectedly while executing the investigation "
                        f"graph. Runtime error: {exc}",
                        [],
                        attempt,
                    ),
                )
                await update_status(work_item_id, RunStatus.ESCALATED)
                await _sync_ado_state(ado, work_item_id, "escalated")
                _finish_trace(
                    RunStatus.ESCALATED.value,
                    conclusion=f"Graph execution failed: {exc}",
                    attempt_count=attempt,
                )
                return

            final_status = final_state.get("status", RunStatus.ESCALATED.value)
            blocked = final_state.get("blocked_tools", [])
            conclusion = _extract_conclusion(final_state)

            log.info(
                "worker.complete",
                status=final_status,
                work_item_id=work_item_id,
                action=action,
            )

            with start_trace_span(
                "worker.publish_result",
                span_type="TASK",
                inputs={"status": final_status, "action": action},
            ) as span:
                if final_status == RunStatus.AWAITING_APPROVAL.value:
                    await ado.add_comment(
                        work_item_id,
                        _format_approval_request(final_state, conclusion),
                    )
                    await update_status(work_item_id, RunStatus.AWAITING_APPROVAL)
                    await _sync_ado_state(ado, work_item_id, "awaiting_approval")
                    set_span_outputs(span, {"status": RunStatus.AWAITING_APPROVAL.value})
                    _finish_trace(
                        RunStatus.AWAITING_APPROVAL.value,
                        conclusion=conclusion,
                        pending_tool_call=final_state.get("pending_tool_call"),
                        attempt_count=attempt,
                    )
                    return

                if final_status == RunStatus.ESCALATED.value:
                    diagnosis_handoff = (
                        action != "resume"
                        and not final_state.get("write_executed", False)
                        and (
                            bool(final_state.get("evidence"))
                            or conclusion != "No textual conclusion was produced."
                        )
                    )
                    await ado.add_comment(
                        work_item_id,
                        _format_diagnosis_handoff(conclusion, final_state, blocked, attempt)
                        if diagnosis_handoff
                        else _format_escalation(
                            conclusion or "The agent could not safely continue.",
                            blocked,
                            attempt,
                        ),
                    )
                    await update_status(work_item_id, RunStatus.ESCALATED)
                    await _sync_ado_state(ado, work_item_id, "escalated")
                    set_span_status(span, "ERROR")
                    set_span_outputs(span, {"status": RunStatus.ESCALATED.value})
                    _finish_trace(
                        RunStatus.ESCALATED.value,
                        conclusion=conclusion,
                        blocked_tools=blocked,
                        attempt_count=attempt,
                    )
                    return

                pending_write = final_state.get("pending_tool_call") or {}

                if action == "resume":
                    if final_status != RunStatus.RESOLVED.value:
                        log.error(
                            "worker.resume_incomplete",
                            work_item_id=work_item_id,
                            status=final_status,
                        )
                        await ado.add_comment(
                            work_item_id,
                            _format_escalation(
                                "The agent resumed after approval but did not reach a resolved "
                                "terminal state. Escalating for human review.\n\n"
                                f"Last model output:\n{conclusion}",
                                blocked,
                                attempt,
                            ),
                        )
                        await update_status(work_item_id, RunStatus.ESCALATED)
                        await _sync_ado_state(ado, work_item_id, "escalated")
                        set_span_status(span, "ERROR")
                        set_span_outputs(span, {"status": RunStatus.ESCALATED.value})
                        _finish_trace(
                            RunStatus.ESCALATED.value,
                            conclusion=conclusion,
                            blocked_tools=blocked,
                            attempt_count=attempt,
                        )
                        return
                    await ado.add_comment(
                        work_item_id,
                        _format_execution_result(conclusion, final_state),
                    )
                    await update_status(work_item_id, RunStatus.RESOLVED)
                    await _sync_ado_state(ado, work_item_id, "resolved")
                    set_span_outputs(span, {"status": RunStatus.RESOLVED.value})
                    _finish_trace(
                        RunStatus.RESOLVED.value,
                        conclusion=conclusion,
                        attempt_count=attempt,
                    )
                    return

                if final_status == RunStatus.RESOLVED.value:
                    await ado.add_comment(work_item_id, _format_diagnosis(conclusion, final_state))
                    await update_status(work_item_id, RunStatus.RESOLVED)
                    await _sync_ado_state(ado, work_item_id, "resolved")
                    set_span_outputs(span, {"status": RunStatus.RESOLVED.value})
                    _finish_trace(
                        RunStatus.RESOLVED.value,
                        conclusion=conclusion,
                        attempt_count=attempt,
                    )
                    return

                await ado.add_comment(work_item_id, _format_diagnosis(conclusion, final_state))
                if pending_write:
                    await update_status(work_item_id, RunStatus.AWAITING_APPROVAL)
                    await _sync_ado_state(ado, work_item_id, "awaiting_approval")
                    set_span_outputs(span, {"status": RunStatus.AWAITING_APPROVAL.value})
                    _finish_trace(
                        RunStatus.AWAITING_APPROVAL.value,
                        conclusion=conclusion,
                        pending_tool_call=pending_write,
                        attempt_count=attempt,
                    )
                    return

                log.error(
                    "worker.unexpected_terminal_status",
                    work_item_id=work_item_id,
                    status=final_status,
                )
                await ado.add_comment(
                    work_item_id,
                    _format_escalation(
                        "The graph reached a terminal state without returning a supported "
                        f"final status. Last output:\n{conclusion}",
                        blocked,
                        attempt,
                    ),
                )
                await update_status(work_item_id, RunStatus.ESCALATED)
                await _sync_ado_state(ado, work_item_id, "escalated")
                set_span_status(span, "ERROR")
                set_span_outputs(span, {"status": RunStatus.ESCALATED.value})
                _finish_trace(
                    RunStatus.ESCALATED.value,
                    conclusion=conclusion,
                    blocked_tools=blocked,
                    attempt_count=attempt,
                )


def _extract_conclusion(state: AgentState) -> str:
    messages = state.get("messages", [])
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = _message_text(message.content)
            if text:
                return text

    pending = state.get("pending_tool_call") or {}
    if pending:
        return (
            "The agent has gathered enough evidence to request approval for a write action: "
            f"`{pending.get('name', 'unknown_tool')}` with arguments "
            f"`{json.dumps(pending.get('args', {}), sort_keys=True)}`."
        )
    return "No textual conclusion was produced."


async def _sync_ado_state(
    ado: AdoClient,
    work_item_id: int,
    stage: AdoWorkflowStage,
) -> None:
    try:
        state = ado_state_for_stage(stage)
    except ValueError as exc:
        log.error(
            "worker.ado_state_mapping_missing",
            work_item_id=work_item_id,
            stage=stage,
            error=str(exc),
        )
        return

    try:
        await ado.update_state(work_item_id, state)
    except Exception as exc:
        log.error(
            "worker.ado_state_update_failed",
            work_item_id=work_item_id,
            stage=stage,
            state=state,
            error=str(exc),
        )


def _latest_retry_feedback_message(comments: list[dict]) -> str | None:
    for comment in reversed(comments):
        text = str(comment.get("text", "")).strip()
        if not text:
            continue
        if "did not work" not in text.lower():
            continue
        return build_approval_context_message(text)
    return None


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


def _format_approval_request(state: AgentState, conclusion: str) -> str:
    fix = state.get("proposed_fix") or {}
    pending = state.get("pending_tool_call") or {}
    pending_batch = state.get("pending_tool_calls") or ([] if not pending else [pending])
    lines = [
        "## Agent Investigation Complete",
        "",
        f"**Attempt:** {state['attempt_count'] + 1} of {settings.max_attempts}",
        f"**Failure type:** `{state.get('failure_type', 'unknown')}`",
        "",
        "### Diagnosis",
        conclusion,
    ]

    if state.get("evidence"):
        lines += ["", "### Evidence collected"]
        for item in state["evidence"][:5]:
            lines.append(f"- {item.get('source', '?')}: {item.get('summary', '')}")

    if pending_batch:
        lines += [
            "",
            "### Pending tool batch",
            "The following tool calls will execute after approval:",
        ]
        for index, tool_call in enumerate(pending_batch, start=1):
            lines.append(f"{index}. `{tool_call.get('name', 'unknown')}`")
            lines.append(
                f"   Args: `{json.dumps(tool_call.get('args', {}), sort_keys=True)}`"
            )

    if fix.get("pr_url"):
        lines += [
            "",
            "### Proposed Fix",
            f"PR: {fix['pr_url']}",
            f"Summary: {fix.get('diff_summary', '')}",
        ]

    lines += [
        "",
        "---",
        "Reply **`approve`** to proceed with the pending tool batch, or **`did not work`** "
        "with the new error message to trigger another investigation pass.",
    ]
    return "\n".join(lines)


def _format_execution_result(conclusion: str, state: AgentState) -> str:
    lines = [
        "## Agent Execution Complete",
        "",
        conclusion,
    ]
    if state.get("evidence"):
        lines += ["", "### Evidence collected"]
        for item in state["evidence"][:5]:
            lines.append(f"- {item.get('source', '?')}: {item.get('summary', '')}")
    lines += [
        "",
        "---",
        "If the applied action did not resolve the incident, reply "
        "**`did not work`** with the new error details.",
    ]
    return "\n".join(lines)


def _format_escalation(conclusion: str, blocked_tools: list[str], attempt: int) -> str:
    lines = [
        "## Agent Escalation",
        "",
        f"The agent was unable to resolve this issue after {attempt + 1} attempt(s).",
        "",
        "### Last hypothesis",
        conclusion,
    ]
    if blocked_tools:
        lines += ["", f"**Blocked tools:** `{'`, `'.join(blocked_tools)}`"]
    lines += [
        "",
        "Manual investigation required. Review the run logs and checkpoint "
        "history for the full tool trace.",
    ]
    return "\n".join(lines)


def _methods_tried(state: AgentState) -> list[str]:
    methods: list[str] = []
    seen: set[str] = set()
    for item in state.get("evidence", []):
        source = str(item.get("source", "")).strip()
        if not source or source in seen:
            continue
        seen.add(source)
        methods.append(source)
    return methods


def _extract_recommended_next_step(conclusion: str) -> str | None:
    paragraphs = [chunk.strip() for chunk in conclusion.split("\n\n") if chunk.strip()]
    for paragraph in paragraphs:
        lowered = paragraph.lower()
        if lowered.startswith("next"):
            return paragraph
        if "recommend" in lowered or "next step" in lowered:
            return paragraph
    return None


def _format_diagnosis_handoff(
    conclusion: str,
    state: AgentState,
    blocked_tools: list[str],
    attempt: int,
) -> str:
    lines = [
        "## Agent Handoff",
        "",
        (
            f"The agent gathered a diagnosis after {attempt + 1} attempt(s), "
            "but did not confirm recovery."
        ),
        "Human follow-up is required.",
        "",
        "### Diagnosis",
        conclusion,
    ]
    if state.get("evidence"):
        lines += ["", "### Evidence collected"]
        for item in state["evidence"][:8]:
            lines.append(f"- {item.get('source', '?')}: {item.get('summary', '')}")
    methods = _methods_tried(state)
    if methods:
        lines += ["", "### Methods tried"]
        for method in methods:
            lines.append(f"- `{method}`")
    pending_batch = state.get("pending_tool_calls") or []
    if pending_batch:
        lines += ["", "### Pending tool batch not executed"]
        for tool_call in pending_batch[:5]:
            lines.append(f"- `{tool_call.get('name', 'unknown')}`")
    elif state.get("pending_tool_call"):
        pending = state["pending_tool_call"]
        lines += [
            "",
            "### Pending tool call not executed",
            f"- `{pending.get('name', 'unknown')}`",
        ]
    if blocked_tools:
        lines += ["", f"**Blocked tools:** `{'`, `'.join(blocked_tools)}`"]
    next_step = _extract_recommended_next_step(conclusion)
    if next_step:
        lines += ["", "### Recommended next step", next_step]
    lines += [
        "",
        (
            "Use the diagnosis, evidence, and methods tried above as the "
            "starting point for manual remediation."
        ),
    ]
    return "\n".join(lines)


def _format_diagnosis(conclusion: str, state: AgentState) -> str:
    pending = state.get("pending_tool_call") or {}
    lines = [
        "## Agent Diagnosis",
        "",
        conclusion,
    ]
    if state.get("evidence"):
        lines += ["", "### Evidence collected"]
        for item in state["evidence"][:5]:
            lines.append(f"- {item.get('source', '?')}: {item.get('summary', '')}")
    if pending:
        lines += [
            "",
            "### Pending write action",
            f"Tool: `{pending.get('name', 'unknown')}`",
            f"Args: `{json.dumps(pending.get('args', {}), sort_keys=True)}`",
            "",
            "---",
            "Reply **`approve`** to continue, or **`did not work`** with the new error to retry.",
        ]
    else:
        lines += [
            "",
            "---",
            "Reply **`did not work`** with the new error details if the diagnosis "
            "was incomplete or incorrect.",
        ]
    return "\n".join(lines)


async def main() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )

    payload_raw = os.environ.get("JOB_PAYLOAD", "")
    try:
        if payload_raw:
            payload = normalize_run_message(json.loads(payload_raw))
            await run_agent(payload["work_item_id"], action=payload["action"])
            return

        handled = await process_next_run_message(_handle_queue_payload)
        if not handled:
            log.info("worker.no_queue_message")
    except Exception as exc:
        log.exception("worker.unhandled_error", error=str(exc))
        sys.exit(1)


async def _handle_queue_payload(payload: dict) -> None:
    normalized = normalize_run_message(payload)
    await run_agent(normalized["work_item_id"], action=normalized["action"])


if __name__ == "__main__":
    asyncio.run(main())
