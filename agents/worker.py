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
from integrations.ado import AdoClient
from mcp_client import load_mcp_tools
from prompts import build_investigation_prompt
from queueing import normalize_run_message, process_next_run_message

log = structlog.get_logger()

WorkerAction = Literal["investigate", "resume"]


async def run_agent(work_item_id: int, *, action: WorkerAction = "investigate") -> None:
    run = await get_run(work_item_id)
    if not run:
        log.error("worker.run_not_found", work_item_id=work_item_id)
        return

    thread_id = run["thread_id"]
    attempt = run["attempt_count"]
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
            await ado.add_comment(
                work_item_id,
                _format_escalation(
                    "The agent reached the configured maximum number of attempts "
                    "before a new investigation could begin.",
                    [],
                    attempt,
                ),
            )
        return

    async with AdoClient() as ado:
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

        await update_status(work_item_id, RunStatus.INVESTIGATING)
        try:
            tools = await load_mcp_tools(target_env=target_env)
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
            return
        graph_def = build_graph(tools)

        async with get_checkpointer() as checkpointer:
            await bootstrap(checkpointer)
            compiled = graph_def.compile(
                checkpointer=checkpointer,
                interrupt_before=["execute_write"],
            )
            config = {"configurable": {"thread_id": thread_id}}

            if action == "resume":
                snapshot = await compiled.aget_state(config)
                if not snapshot.next:
                    log.warning("worker.no_pending_checkpoint", work_item_id=work_item_id)
                    await ado.add_comment(
                        work_item_id,
                        "No pending approval step was found to resume. The agent run "
                        "may have already completed.",
                    )
                    return
                final_state = await compiled.ainvoke(None, config)
            else:
                initial_message = build_investigation_prompt(
                    work_item=work_item,
                    comments=comments,
                    attempt=attempt,
                )
                initial_state: AgentState = {
                    "messages": [HumanMessage(content=initial_message)],
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
                    "blocked_tools": [],
                }
                final_state = await compiled.ainvoke(initial_state, config)

        final_status = final_state.get("status", RunStatus.ESCALATED.value)
        blocked = final_state.get("blocked_tools", [])
        conclusion = _extract_conclusion(final_state)

        log.info("worker.complete", status=final_status, work_item_id=work_item_id, action=action)

        if final_status == RunStatus.AWAITING_APPROVAL.value:
            await ado.add_comment(work_item_id, _format_approval_request(final_state, conclusion))
            await update_status(work_item_id, RunStatus.AWAITING_APPROVAL)
            return

        if final_status == RunStatus.ESCALATED.value:
            await ado.add_comment(
                work_item_id,
                _format_escalation(
                    conclusion or "The agent could not safely continue.",
                    blocked,
                    attempt,
                ),
            )
            await update_status(work_item_id, RunStatus.ESCALATED)
            return

        if action == "resume":
            await ado.add_comment(work_item_id, _format_execution_result(conclusion, final_state))
            await update_status(work_item_id, RunStatus.RESOLVED)
            return

        await ado.add_comment(work_item_id, _format_diagnosis(conclusion, final_state))
        await update_status(work_item_id, RunStatus.AWAITING_APPROVAL)


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

    if pending:
        lines += [
            "",
            "### Pending write action",
            f"Tool: `{pending.get('name', 'unknown')}`",
            f"Args: `{json.dumps(pending.get('args', {}), sort_keys=True)}`",
        ]

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
        "Reply **`approve`** to proceed with the pending write action, or **`did not work`** "
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
