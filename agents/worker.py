"""
Cloud Run Job entry point.

This is what runs inside the Agent Worker container.
Triggered by Pub/Sub via Cloud Run Job execution.

Environment variable JOB_PAYLOAD is set by the Pub/Sub push subscription
and contains: {"work_item_id": 123, "run_id": "uuid"}
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import structlog
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agents.graph import AgentState, build_graph
from mcp_client import load_mcp_tools
from prompts import build_investigation_prompt
from config import settings
from db import RunStatus, bootstrap, get_checkpointer, get_pool, get_run, update_status
from integrations.ado import AdoClient

log = structlog.get_logger()


async def run_agent(work_item_id: int) -> None:
    """Full agent lifecycle for one work item."""

    # ── Load run state from Neon ──────────────────────────────────────────────
    run = await get_run(work_item_id)
    if not run:
        log.error("worker.run_not_found", work_item_id=work_item_id)
        return

    thread_id = run["thread_id"]
    attempt   = run["attempt_count"]

    log.info("worker.start", work_item_id=work_item_id, thread_id=thread_id, attempt=attempt)

    # Check attempt cap
    if attempt >= settings.max_attempts:
        log.warning("worker.max_attempts_reached", work_item_id=work_item_id)
        await update_status(work_item_id, RunStatus.ESCALATED)
        return

    # ── Hydrate context from ADO work item ────────────────────────────────────
    ado = AdoClient()
    work_item   = await ado.get_work_item(work_item_id)
    comments    = await ado.get_comments(work_item_id)

    # Build the initial investigation message
    initial_message = build_investigation_prompt(
        work_item=work_item,
        comments=comments,
        attempt=attempt,
    )

    await update_status(work_item_id, RunStatus.INVESTIGATING)

    # ── Load MCP tools ────────────────────────────────────────────────────────
    # MCP servers run as sidecars - connect over localhost stdio
    tools = await load_mcp_tools(
        target_env=work_item.get("target_env", "databricks"),
    )
    log.info("worker.tools_loaded", count=len(tools))

    # ── Build graph + checkpointer ────────────────────────────────────────────
    graph_def = build_graph(tools)

    async with get_checkpointer() as checkpointer:
        await bootstrap(checkpointer)

        compiled = graph_def.compile(
            checkpointer=checkpointer,
            # Pause before any WRITE tool - wait for ADO "approve" comment
            interrupt_before=["execute_write"],
        )

        config = {"configurable": {"thread_id": thread_id}}

        # ── Initial state ─────────────────────────────────────────────────────
        initial_state: AgentState = {
            "messages":       [HumanMessage(content=initial_message)],
            "run_id":         str(run["run_id"]),
            "work_item_id":   work_item_id,
            "thread_id":      thread_id,
            "attempt_count":  attempt,
            "failure_type":   work_item.get("failure_type", "unknown"),
            "target_env":     work_item.get("target_env", "databricks"),
            "hypothesis":     "",
            "evidence":       [],
            "iteration_count": 0,
            "status":         RunStatus.INVESTIGATING,
            "retries":        0,
            "proposed_fix":   None,
            "write_approved": False,
            "blocked_tools":  [],
        }

        # ── If this is a retry, restore previous state from checkpoint ────────
        existing = await checkpointer.aget(config)
        if existing and attempt > 0:
            log.info("worker.resuming_from_checkpoint", thread_id=thread_id)
            # Append new feedback to existing messages instead of replacing state
            final_state = await compiled.ainvoke(
                {"messages": [HumanMessage(content=initial_message)]},
                config,
            )
        else:
            final_state = await compiled.ainvoke(initial_state, config)

        # ── Handle output ─────────────────────────────────────────────────────
        final_status = final_state.get("status", RunStatus.ESCALATED)
        log.info("worker.complete", status=final_status, work_item_id=work_item_id)

        # Extract the agent's final message
        last_msg = final_state["messages"][-1]
        conclusion = getattr(last_msg, "content", str(last_msg))

        if final_status == RunStatus.AWAITING_APPROVAL:
            # Post the proposed fix as an ADO comment and wait
            await ado.add_comment(
                work_item_id,
                _format_approval_request(final_state, conclusion),
            )
            await update_status(work_item_id, RunStatus.AWAITING_APPROVAL)

        elif final_status == RunStatus.ESCALATED:
            blocked = final_state.get("blocked_tools", [])
            await ado.add_comment(
                work_item_id,
                _format_escalation(conclusion, blocked, attempt),
            )
            await update_status(work_item_id, RunStatus.ESCALATED)

        else:
            # Agent produced a diagnosis - post it
            await ado.add_comment(
                work_item_id,
                _format_diagnosis(conclusion, final_state),
            )
            await update_status(work_item_id, RunStatus.AWAITING_APPROVAL)


def _format_approval_request(state: AgentState, conclusion: str) -> str:
    blocked = state.get("blocked_tools", [])
    fix = state.get("proposed_fix") or {}
    lines = [
        "## Agent Investigation Complete",
        "",
        f"**Attempt:** {state['attempt_count'] + 1} of {settings.max_attempts}",
        f"**Failure type:** `{state.get('failure_type', 'unknown')}`",
        "",
        "### Diagnosis",
        conclusion,
        "",
    ]
    if fix.get("pr_url"):
        lines += [
            "### Proposed Fix",
            f"PR: {fix['pr_url']}",
            f"Summary: {fix.get('diff_summary', '')}",
            "",
        ]
    lines += [
        "---",
        "Reply **`approve`** to proceed with the fix, or **`did not work`** "
        "with the new error message to trigger another investigation pass.",
    ]
    return "\n".join(lines)


def _format_escalation(conclusion: str, blocked_tools: list, attempt: int) -> str:
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
        "Manual investigation required. Full tool call trace available in LangSmith.",
    ]
    return "\n".join(lines)


def _format_diagnosis(conclusion: str, state: AgentState) -> str:
    evidence = state.get("evidence", [])
    lines = [
        "## Agent Diagnosis",
        "",
        conclusion,
    ]
    if evidence:
        lines += ["", "### Evidence collected"]
        for item in evidence[:5]:  # cap at 5 to keep comment readable
            lines.append(f"- {item.get('source', '?')}: {item.get('summary', '')}")
    lines += [
        "",
        "---",
        "Reply **`approve`** to accept, or **`did not work`** with the error to retry.",
    ]
    return "\n".join(lines)


# ── Entry point ────────────────────────────────────────────────────────────────

async def main() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )

    payload_raw = os.environ.get("JOB_PAYLOAD", "")
    if not payload_raw:
        log.error("worker.missing_payload")
        sys.exit(1)

    try:
        payload = json.loads(payload_raw)
        work_item_id = int(payload["work_item_id"])
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        log.error("worker.invalid_payload", error=str(exc))
        sys.exit(1)

    try:
        await run_agent(work_item_id)
    except Exception as exc:
        log.exception("worker.unhandled_error", error=str(exc))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
