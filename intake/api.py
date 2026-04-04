"""
Azure-hosted intake API.

Receives Azure DevOps Service Hook webhooks, validates signatures, stores
idempotency state in Neon, and enqueues worker runs via Azure Service Bus.
Also accepts raw Databricks and Azure Monitor alerts over HTTP.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import asynccontextmanager
from typing import Literal

import structlog
from fastapi import FastAPI, HTTPException, Request, Response

from config import settings
from db import (
    begin_approval_resume,
    bootstrap_db,
    close_pool,
    ensure_run,
    get_pool,
    queue_retry,
    record_processed_event,
)
from glue.normaliser import process_alert_webhook
from integrations.ado import validate_service_hook_signature
from queueing import build_run_message, is_queue_configured, publish_run_message

log = structlog.get_logger()

RunAction = Literal["investigate", "resume"]
CommentAction = Literal["approve", "retry", "ignore"]


@asynccontextmanager
async def lifespan(_: FastAPI):
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    await bootstrap_db()
    await get_pool()

    if is_queue_configured():
        log.info(
            "intake.queue_ready",
            queue=settings.azure_service_bus_queue_name,
        )
    else:
        log.warning(
            "intake.queue_disabled",
            reason=(
                "AZURE_SERVICE_BUS_CONNECTION_STRING or AZURE_SERVICE_BUS_NAMESPACE "
                "must be set"
            ),
        )

    try:
        yield
    finally:
        await close_pool()


app = FastAPI(title="Pipeline Resolver Intake API", lifespan=lifespan)


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── ADO webhook handler ──────────────────────────────────────────────────────

@app.post("/webhooks/ado")
async def ado_webhook(request: Request) -> Response:
    body = await request.body()

    sig = request.headers.get("X-Hub-Signature")
    if not validate_service_hook_signature(body, sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    event_type = payload.get("eventType", "")
    work_item_id = _extract_work_item_id(payload)
    event_id = _extract_event_id(payload, body)

    should_process = await record_processed_event(
        event_id=event_id,
        event_type=event_type or "unknown",
        work_item_id=work_item_id,
    )
    if not should_process:
        log.info("intake.duplicate_event_ignored", event_id=event_id, event_type=event_type)
        return Response(content='{"status":"ignored"}', media_type="application/json")

    log.info("intake.webhook_received", event_type=event_type, work_item_id=work_item_id)

    if event_type == "workitem.created":
        await _handle_created(payload)
    elif event_type == "workitem.commented":
        await _handle_commented(payload)
    else:
        log.debug("intake.event_ignored", event_type=event_type)

    return Response(content='{"status":"queued"}', media_type="application/json")


@app.post("/webhooks/alerts")
async def alerts_webhook(request: Request) -> Response:
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    result = await process_alert_webhook(payload, headers=request.headers)
    return Response(content=json.dumps(result), media_type="application/json")


async def _handle_created(payload: dict) -> None:
    resource = payload.get("resource", {})
    work_item_id = resource.get("id")
    work_item_url = resource.get("url", "")
    fields = resource.get("fields", {})
    tags = fields.get("System.Tags", "")

    if not work_item_id:
        log.warning("intake.created_missing_work_item_id")
        return

    if "agent:run" not in tags.lower():
        log.debug("intake.not_agent_tagged", work_item_id=work_item_id)
        return

    run = await ensure_run(work_item_id=work_item_id, work_item_url=work_item_url)
    await _enqueue(run, action="investigate")
    log.info("intake.run_queued", work_item_id=work_item_id, run_id=run["run_id"])


async def _handle_commented(payload: dict) -> None:
    resource = payload.get("resource", {})
    work_item_id = resource.get("workItemId") or resource.get("id")
    comment_text = _extract_comment_text(resource)
    action = parse_comment_action(comment_text)

    if not work_item_id or action == "ignore":
        log.debug("intake.comment_ignored", work_item_id=work_item_id)
        return

    if action == "approve":
        run = await begin_approval_resume(work_item_id=work_item_id)
        if run is None:
            log.info("intake.approval_ignored", work_item_id=work_item_id)
            return
        await _enqueue(run, action="resume")
        log.info("intake.approval_queued", work_item_id=work_item_id)
        return

    run = await queue_retry(work_item_id=work_item_id)
    if run is None:
        log.info("intake.retry_ignored", work_item_id=work_item_id)
        return
    await _enqueue(run, action="investigate")
    log.info("intake.retry_queued", work_item_id=work_item_id, attempt=run["attempt_count"])


async def _enqueue(run: dict, *, action: RunAction) -> None:
    if not is_queue_configured():
        log.warning("intake.queue_not_configured", run_id=run["run_id"], action=action)
        return

    await publish_run_message(build_run_message(run, action=action))
    log.info(
        "intake.enqueued",
        run_id=run["run_id"],
        action=action,
        queue=settings.azure_service_bus_queue_name,
    )


def parse_comment_action(comment_text: str) -> CommentAction:
    text = comment_text.lower().strip()
    if re.search(r"\bdid not work\b", text):
        return "retry"
    if re.search(r"\bapprove(d)?\b", text):
        return "approve"
    return "ignore"


def _extract_comment_text(resource: dict) -> str:
    for value in (
        resource.get("comment", {}).get("text"),
        resource.get("text"),
        resource.get("message", {}).get("text"),
    ):
        if value:
            return str(value)
    return ""


def _extract_work_item_id(payload: dict) -> int | None:
    resource = payload.get("resource", {})
    work_item_id = resource.get("workItemId") or resource.get("id")
    try:
        return int(work_item_id) if work_item_id is not None else None
    except (TypeError, ValueError):
        return None


def _extract_event_id(payload: dict, body: bytes) -> str:
    payload_id = payload.get("id")
    if payload_id:
        return str(payload_id)
    return hashlib.sha256(body).hexdigest()
