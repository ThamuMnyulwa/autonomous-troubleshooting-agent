"""
Alert normalisation utilities.

Receives raw Databricks and Azure Monitor alerts, normalises them, and creates
Azure DevOps bug work items for the troubleshooting agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping

import structlog

from config import settings
from db import attach_alert_work_item, bootstrap_db, claim_alert_dedup
from integrations.ado import AdoClient, build_bug_description

log = structlog.get_logger()


def _parse_azure_target(target: str) -> dict[str, str]:
    raw_target = str(target).strip()
    parsed = {
        "incident_target": raw_target,
        "target_resource_id": "",
        "target_resource_name": raw_target,
        "target_resource_group": "",
        "target_resource_type": "",
        "target_subscription": "",
    }
    if not raw_target.startswith("/"):
        return parsed

    parts = [part for part in raw_target.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "subscriptions":
        return parsed

    parsed["target_resource_id"] = raw_target
    parsed["target_subscription"] = parts[1]
    for idx, part in enumerate(parts):
        lowered = part.lower()
        if lowered == "resourcegroups" and idx + 1 < len(parts):
            parsed["target_resource_group"] = parts[idx + 1]
        if lowered == "providers" and idx + 1 < len(parts):
            provider_namespace = parts[idx + 1]
            resource_segments = parts[idx + 2 :]
            type_segments = resource_segments[0::2]
            name_segments = resource_segments[1::2]
            if type_segments:
                parsed["target_resource_type"] = "/".join([provider_namespace, *type_segments])
            if name_segments:
                parsed["target_resource_name"] = "/".join(name_segments)
            break
    return parsed


def detect_source(body: dict, headers: Mapping[str, str] | None = None) -> str:
    user_agent = ""
    if headers is not None:
        user_agent = headers.get("User-Agent", "")
    if "Databricks" in user_agent or "event_type" in body or "run" in body:
        return "databricks"
    if body.get("schemaId") == "azureMonitorCommonAlertSchema":
        return "azure"
    return "unknown"


async def process_alert_webhook(
    body: dict,
    *,
    headers: Mapping[str, str] | None = None,
) -> dict[str, str | int]:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    source = detect_source(body, headers)
    log.info("alerts.received", source=source)
    work_item_id = await _process(body, source)
    if work_item_id:
        return {"work_item_id": work_item_id}
    return {"status": "suppressed"}


def process_alert_webhook_sync(
    body: dict,
    *,
    headers: Mapping[str, str] | None = None,
) -> dict[str, str | int]:
    return asyncio.run(process_alert_webhook(body, headers=headers))


async def _process(body: dict, source: str) -> int | None:
    await bootstrap_db()

    if source == "databricks":
        alert = _parse_databricks(body)
    elif source == "azure":
        alert = _parse_azure(body)
    else:
        log.warning("glue.unknown_source")
        return None

    if not alert:
        return None

    dedup_key = make_alert_dedup_key(alert["job_id"], alert["error_type"])
    should_create = await claim_alert_dedup(dedup_key)
    if not should_create:
        log.info("glue.duplicate_suppressed", job_id=alert.get("job_id"))
        return None

    async with AdoClient() as ado:
        wi = await ado.create_bug(
            title=alert["title"],
            description_html=build_bug_description(
                **alert["description_kwargs"],
                failure_type=alert["error_type"],
            ),
            tags=alert["tags"],
        )

    await attach_alert_work_item(dedup_key, wi["id"])
    return wi["id"]


def _parse_databricks(body: dict) -> dict | None:
    run = body.get("run", {})
    state = run.get("state", {})

    if state.get("result_state") != "FAILED":
        return None

    job_id = run.get("job_id", "?")
    run_id = run.get("run_id", "?")

    task_runs = run.get("task_runs", [])
    failed_task = "unknown"
    error_msg = state.get("state_message", "")

    for task in task_runs:
        if task.get("state", {}).get("result_state") == "FAILED":
            failed_task = task.get("task_key", "unknown")
            error_msg = task.get("error", error_msg)
            break

    workspace_url = run.get("run_page_url", settings.databricks_host)
    workspace_url = re.sub(r"/run/.*", "", workspace_url)
    failure_type = _classify_error(error_msg)

    return {
        "title": f"Databricks Job {job_id} failed: {failed_task} - {error_msg[:80]}",
        "tags": "agent:run; databricks; SEV2",
        "job_id": job_id,
        "error_type": failure_type,
        "description_kwargs": {
            "environment": "databricks",
            "workspace_url": workspace_url,
            "job_id": job_id,
            "run_id": run_id,
            "failed_task": failed_task,
            "error_message": error_msg,
        },
    }


def _parse_azure(body: dict) -> dict | None:
    data = body.get("data", {})
    essentials = data.get("essentials", {})
    context = data.get("alertContext", {})

    if essentials.get("monitorCondition") != "Fired":
        return None

    rule = essentials.get("alertRule", "unknown")
    config_items = essentials.get("configurationItems") or ["unknown"]
    target = config_items[0]
    target_meta = _parse_azure_target(target)
    desc = context.get("ResultDescription", "")
    sev = essentials.get("severity", "Sev3")
    error_msg = desc if desc else f"Azure Monitor alert fired: {rule}"

    return {
        "title": f"Azure Monitor: {rule} on {target}",
        "tags": f"agent:run; azure; {sev.lower()}",
        "job_id": rule,
        "error_type": _classify_error(error_msg),
        "description_kwargs": {
            "environment": "azure",
            "workspace_url": essentials.get("alertId", ""),
            "job_id": rule,
            "run_id": essentials.get("firedDateTime", ""),
            "failed_task": target,
            "error_message": error_msg,
            "alert_rule": rule,
            **target_meta,
        },
    }


def make_alert_dedup_key(job_id: str | int, error_type: str) -> str:
    raw = f"{job_id}:{error_type}"
    return hashlib.md5(raw.encode()).hexdigest()


def _classify_error(error_msg: str) -> str:
    msg = error_msg.lower()
    if any(k in msg for k in ("schema", "column", "cannot resolve", "analysisexception")):
        return "schema_mismatch"
    if any(k in msg for k in ("timeout", "connection", "network", "nsg")):
        return "infra"
    if any(k in msg for k in ("authentication", "unauthorized", "403", "secret", "credential")):
        return "auth_failure"
    if any(k in msg for k in ("memory", "oom", "heap", "gc overhead")):
        return "resource"
    return "unknown"
