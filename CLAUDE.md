# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Autonomous data pipeline troubleshooting agent (`pipeline-resolver`) for Azure/Databricks environments. Uses LangGraph + Gemini + MCP to investigate pipeline failures, propose fixes via Azure DevOps work items, and pause for human approval before executing writes.

## Commands

```bash
# Setup
uv venv && source .venv/bin/activate
uv sync

# Lint & format
ruff check .
ruff format .
mypy .

# Test
pytest                # asyncio_mode=auto, testpaths=["tests"]
pytest tests/test_x.py::test_name   # single test

# Run locally
uvicorn intake.api:app --reload --port 8080          # intake API
JOB_PAYLOAD='{"work_item_id": 123}' python agents/worker.py  # one-off worker run
python agents/worker.py                                      # queue-driven worker
```

## Architecture

```
Alert Sources → FastAPI alert route → ADO Bug work item
                                         ↓
                    FastAPI Intake on Azure Container Apps ← ADO Service Hook webhook
                                         ↓
                         Neon PostgreSQL + Azure Service Bus queue
                                         ↓
                    Queue-driven worker on Azure Container Apps Job
                              ↓                      ↓
                      MCP Tool Servers      LangGraph ReAct Loop
                                                     ↓
                          ADO comment (diagnosis/approval request/escalation)
```

**Key modules:**

- `agents/graph.py` — Custom LangGraph StateGraph with guardrail node intercepting all tool calls. Risk levels: READ (safe), WRITE (needs human approval via `interrupt_before`), HARD_BLOCK (immediate escalation). Uses `execute_write` interrupt node for HITL.
- `agents/worker.py` — Queue-driven worker entry point for Azure-hosted execution. Loads run state from Neon, fetches ADO work item, builds prompt, loads MCP tools, invokes graph, posts results as ADO comments. Supports checkpoint resumption on retries.
- `intake/api.py` — FastAPI receiver for ADO Service Hooks and raw alert webhooks. Validates HMAC-SHA256, routes `workitem.created` (with `agent:run` tag), `workitem.commented` ("did not work" → retry), and alert payloads into ADO bugs.
- `glue/normaliser.py` — Alert normalization utilities for Databricks Job and Azure Monitor webhooks. Includes 5-minute deduplication window and error classification.
- `queueing.py` — Azure Service Bus integration for publishing run envelopes and draining one queue message per worker execution.
- `integrations/ado.py` — Async Azure DevOps REST client (httpx). HMAC validation, work item CRUD, comment management.
- `config.py` — Pydantic Settings loading all config from env vars / `.env`.
- `db.py` — Neon PostgreSQL layer with `AsyncConnectionPool`. Schema: `agent_state.agent_runs`. LangGraph checkpoints in `public.*`.
- `mcp_client.py` — Dynamic MCP tool loader (Databricks, Azure, ADO servers via stdio transport).
- `prompts.py` — Prompt builders wrapping all user content in UNTRUSTED markers to prevent prompt injection.

## Important Design Decisions

- **Custom StateGraph instead of `create_react_agent`**: Gemini's `MALFORMED_FUNCTION_CALL` finish_reason silently breaks default ReAct loops. The custom graph checks `finish_reason` explicitly and retries with corrective messages (up to 3x).
- **Temperature 0.1 not 0.0**: Temperature 0.0 triggers deterministic MALFORMED_FUNCTION_CALL errors with Gemini (LangGraph issue #6574).
- **Neon free tier constraints**: Pool size limited to 5 connections, reconnect logic handles 5-min idle suspension.
- **MCP tools discovered dynamically** via `list_tools()` each run — Databricks explicitly discourages hardcoding tool names.
- **All I/O is async** throughout (database, HTTP, Service Bus).

## Code Style

- Python >=3.12, ruff line-length 100, ruff rules: E, F, I, UP
- Structured logging via `structlog`
- Type checking: mypy (non-strict, ignores missing imports)
