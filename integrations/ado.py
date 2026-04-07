"""
Azure DevOps integration.

Handles:
  - Work item CRUD (read description, post comments, update state)
  - HMAC-SHA256 validation for incoming Service Hook payloads
  - Work item creation from normalised alert payloads (used by the Glue Function)

Auth: PAT encoded as Basic auth header. Store in Secret Manager.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
from typing import Literal

import httpx
import structlog

from config import settings
from prompts.helpers import parse_agent_metadata

log = structlog.get_logger()
_API = "7.1"

AdoWorkflowStage = Literal[
    "queued",
    "investigating",
    "awaiting_approval",
    "resolved",
    "escalated",
]

_DEFAULT_STATES_BY_WORK_ITEM_TYPE: dict[str, dict[AdoWorkflowStage, str]] = {
    "issue": {
        "queued": "To Do",
        "investigating": "Doing",
        "awaiting_approval": "Doing",
        "resolved": "Done",
        "escalated": "Done",
    },
    "task": {
        "queued": "To Do",
        "investigating": "Doing",
        "awaiting_approval": "Doing",
        "resolved": "Done",
        "escalated": "Done",
    },
    "bug": {
        "queued": "New",
        "investigating": "Active",
        "awaiting_approval": "Active",
        "resolved": "Resolved",
        "escalated": "Resolved",
    },
    "product backlog item": {
        "queued": "New",
        "investigating": "Committed",
        "awaiting_approval": "Committed",
        "resolved": "Done",
        "escalated": "Done",
    },
}


def _auth_header(pat: str) -> dict[str, str]:
    token = base64.b64encode(f":{pat}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def _patch_header(pat: str) -> dict[str, str]:
    token = base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json-patch+json",  # required for work item writes
    }


class AdoClient:
    """Thin async wrapper around ADO REST API v7.1."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        project: str | None = None,
        pat: str | None = None,
    ) -> None:
        self._base = (base_url or settings.ado_org_url).rstrip("/")
        self._project = project or settings.ado_project
        self._pat = pat or settings.ado_pat
        self._client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)

    async def __aenter__(self) -> AdoClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_work_item(self, work_item_id: int) -> dict:
        url = f"{self._base}/{self._project}/_apis/wit/workitems/{work_item_id}"
        r = await self._client.get(
            url,
            params={"$expand": "all", "api-version": _API},
            headers=_auth_header(self._pat),
        )
        r.raise_for_status()
        wi = r.json()
        fields = wi.get("fields", {})

        # Extract structured metadata from description
        desc = fields.get("System.Description", "")
        target_env  = _extract_tag(fields.get("System.Tags", ""), r"databricks|azure")
        meta = parse_agent_metadata(desc)
        failure_type = meta.get("failure_type", "")

        return {
            "id":           wi["id"],
            "url":          wi.get("url", ""),
            "fields":       fields,
            "target_env":   target_env or "databricks",
            "failure_type": failure_type or "unknown",
            "incident_target": meta.get("incident_target", ""),
            "alert_rule": meta.get("alert_rule", ""),
            "target_resource_id": meta.get("target_resource_id", ""),
            "target_resource_name": meta.get("target_resource_name", ""),
            "target_resource_group": meta.get("target_resource_group", ""),
            "target_resource_type": meta.get("target_resource_type", ""),
            "target_subscription": meta.get("target_subscription", ""),
        }

    async def get_comments(self, work_item_id: int) -> list[dict]:
        url = (
            f"{self._base}/{self._project}/_apis/wit/workItems/{work_item_id}/comments"
        )
        r = await self._client.get(
            url,
            params={"api-version": "7.1-preview.4"},
            headers=_auth_header(self._pat),
        )
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json().get("comments", [])

    # ── Write ─────────────────────────────────────────────────────────────────

    async def add_comment(self, work_item_id: int, text: str) -> dict:
        url = (
            f"{self._base}/{self._project}/_apis/wit/workItems/{work_item_id}/comments"
        )
        r = await self._client.post(
            url,
            params={"api-version": "7.1-preview.4"},
            headers=_auth_header(self._pat),
            json={"text": text},
        )
        r.raise_for_status()
        log.info("ado.comment_posted", work_item_id=work_item_id)
        return r.json()

    async def update_state(self, work_item_id: int, state: str) -> dict:
        """Update System.State to a workflow-specific value configured for this project."""
        url = f"{self._base}/{self._project}/_apis/wit/workitems/{work_item_id}"
        r = await self._client.patch(
            url,
            params={"api-version": _API},
            headers=_patch_header(self._pat),
            json=[{"op": "add", "path": "/fields/System.State", "value": state}],
        )
        r.raise_for_status()
        return r.json()

    async def create_bug(
        self,
        title: str,
        description_html: str,
        tags: str = "agent:run",
    ) -> dict:
        """
        Create a new work item from an incoming alert.
        Uses the configured work item type and its corresponding workflow defaults.
        """
        wit = settings.ado_work_item_type
        initial_state = ado_state_for_stage("queued", work_item_type=wit, allow_missing=True)
        url = f"{self._base}/{self._project}/_apis/wit/workitems/${wit}"
        body = [
            {"op": "add", "path": "/fields/System.Title",       "value": title},
            {"op": "add", "path": "/fields/System.Description", "value": description_html},
            {"op": "add", "path": "/fields/System.Tags",        "value": tags},
        ]
        if initial_state:
            body.append({"op": "add", "path": "/fields/System.State", "value": initial_state})
        r = await self._client.post(
            url,
            params={"api-version": _API},
            headers=_patch_header(self._pat),
            json=body,
        )
        r.raise_for_status()
        wi = r.json()
        log.info("ado.bug_created", work_item_id=wi["id"], title=title)
        return wi


# ── HMAC validation ────────────────────────────────────────────────────────────

def validate_service_hook_signature(
    body: bytes,
    header_signature: str | None,
    *,
    secret: str | None = None,
) -> bool:
    """
    Validate the HMAC-SHA256 signature on an ADO Service Hook payload.
    ADO sends: X-Hub-Signature: sha256=<hex_digest>
    Return False if signature is missing or invalid.
    """
    if not header_signature:
        log.warning("ado.missing_signature")
        return False

    expected_prefix = "sha256="
    if not header_signature.startswith(expected_prefix):
        return False

    received_digest = header_signature[len(expected_prefix):]
    expected_digest = hmac.new(
        (secret or settings.ado_service_hook_secret).encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    valid = hmac.compare_digest(received_digest, expected_digest)
    if not valid:
        log.warning("ado.invalid_signature")
    return valid


def ado_state_map(work_item_type: str | None = None) -> dict[AdoWorkflowStage, str]:
    """
    Resolve the ADO workflow states used by the agent.

    Defaults are derived from the selected work item type, and each stage can be
    overridden explicitly via environment variables for custom workflows.
    """
    resolved_type = (work_item_type or settings.ado_work_item_type).strip().lower()
    states = dict(_DEFAULT_STATES_BY_WORK_ITEM_TYPE.get(resolved_type, {}))

    overrides: dict[AdoWorkflowStage, str] = {
        "queued": settings.ado_state_queued.strip(),
        "investigating": settings.ado_state_investigating.strip(),
        "awaiting_approval": settings.ado_state_awaiting_approval.strip(),
        "resolved": settings.ado_state_resolved.strip(),
        "escalated": settings.ado_state_escalated.strip(),
    }
    for stage, state in overrides.items():
        if state:
            states[stage] = state

    return states


def ado_state_for_stage(
    stage: AdoWorkflowStage,
    *,
    work_item_type: str | None = None,
    allow_missing: bool = False,
) -> str | None:
    """
    Return the configured ADO System.State for a lifecycle stage.

    For unknown custom workflows, `queued` may be omitted so ADO applies the
    work item's default initial state. Other stages require either a known work
    item type default or explicit `ADO_STATE_*` overrides.
    """
    state = ado_state_map(work_item_type).get(stage, "").strip()
    if state:
        return state
    if allow_missing:
        return None

    wit = work_item_type or settings.ado_work_item_type
    msg = (
        f"No Azure DevOps state is configured for stage '{stage}' and work item type "
        f"{wit!r}. Set the corresponding ADO_STATE_* environment variables."
    )
    raise ValueError(msg)


# ── Alert normaliser helpers ───────────────────────────────────────────────────

def build_bug_description(
    environment: str,
    workspace_url: str,
    job_id: str | int,
    run_id: str | int,
    failed_task: str,
    error_message: str,
    failure_type: str = "unknown",
    target_table: str = "",
    source: str = "",
    alert_rule: str = "",
    incident_target: str = "",
    target_resource_id: str = "",
    target_resource_name: str = "",
    target_resource_group: str = "",
    target_resource_type: str = "",
    target_subscription: str = "",
) -> str:
    """
    Build the HTML description for the ADO Bug work item.
    Includes a machine-readable HTML comment block for agent parsing.
    """
    escaped_environment = html.escape(str(environment))
    escaped_workspace_href = html.escape(str(workspace_url), quote=True)
    escaped_workspace_text = html.escape(str(workspace_url))
    escaped_job_id = html.escape(str(job_id))
    escaped_run_id = html.escape(str(run_id))
    escaped_failed_task = html.escape(str(failed_task))
    escaped_target_table = html.escape(str(target_table))
    escaped_source = html.escape(str(source))
    escaped_alert_rule = html.escape(str(alert_rule))
    escaped_incident_target = html.escape(str(incident_target))
    escaped_target_resource_id = html.escape(str(target_resource_id))
    escaped_target_resource_name = html.escape(str(target_resource_name))
    escaped_target_resource_group = html.escape(str(target_resource_group))
    escaped_target_resource_type = html.escape(str(target_resource_type))
    escaped_target_subscription = html.escape(str(target_subscription))
    escaped_error_message = html.escape(str(error_message))
    workspace_row = (
        f'<tr><td><b>Workspace</b></td><td><a href="{escaped_workspace_href}">'
        f"{escaped_workspace_text}</a></td></tr>"
    )
    extra_rows = []
    if escaped_alert_rule:
        extra_rows.append(f"<tr><td><b>Alert rule</b></td><td>{escaped_alert_rule}</td></tr>")
    if escaped_incident_target:
        extra_rows.append(
            "<tr><td><b>Incident target</b></td><td><code>"
            f"{escaped_incident_target}</code></td></tr>"
        )
    if escaped_target_resource_id:
        extra_rows.append(
            "<tr><td><b>Target resource ID</b></td><td><code>"
            f"{escaped_target_resource_id}</code></td></tr>"
        )
    if escaped_target_resource_name:
        extra_rows.append(
            "<tr><td><b>Target resource name</b></td><td><code>"
            f"{escaped_target_resource_name}</code></td></tr>"
        )
    if escaped_target_resource_group:
        extra_rows.append(
            "<tr><td><b>Target resource group</b></td><td><code>"
            f"{escaped_target_resource_group}</code></td></tr>"
        )
    if escaped_target_resource_type:
        extra_rows.append(
            "<tr><td><b>Target resource type</b></td><td><code>"
            f"{escaped_target_resource_type}</code></td></tr>"
        )
    if escaped_target_subscription:
        extra_rows.append(
            "<tr><td><b>Target subscription</b></td><td><code>"
            f"{escaped_target_subscription}</code></td></tr>"
        )
    extra_rows_html = "\n".join(extra_rows)
    metadata_lines = [
        f"environment: {escaped_environment}",
        f"workspace_url: {escaped_workspace_text}",
        f"job_id: {escaped_job_id}",
        f"run_id: {escaped_run_id}",
        f"failed_task: {escaped_failed_task}",
        f"target_table: {escaped_target_table}",
        f"failure_type: {html.escape(str(failure_type))}",
    ]
    optional_metadata = {
        "source": escaped_source,
        "alert_rule": escaped_alert_rule,
        "incident_target": escaped_incident_target,
        "target_resource_id": escaped_target_resource_id,
        "target_resource_name": escaped_target_resource_name,
        "target_resource_group": escaped_target_resource_group,
        "target_resource_type": escaped_target_resource_type,
        "target_subscription": escaped_target_subscription,
    }
    for key, value in optional_metadata.items():
        if value:
            metadata_lines.append(f"{key}: {value}")

    html_desc = f"""<div>
<h3>Pipeline Failure Report</h3>
<table>
<tr><td><b>Environment</b></td><td>{escaped_environment}</td></tr>
{workspace_row}
<tr><td><b>Job ID</b></td><td>{escaped_job_id}</td></tr>
<tr><td><b>Run ID</b></td><td>{escaped_run_id}</td></tr>
<tr><td><b>Failed task</b></td><td><code>{escaped_failed_task}</code></td></tr>
<tr><td><b>Target table</b></td><td><code>{escaped_target_table}</code></td></tr>
<tr><td><b>Source</b></td><td>{escaped_source}</td></tr>
{extra_rows_html}
</table>

<h4>Error Message</h4>
<pre>{escaped_error_message}</pre>
</div>

<!-- agent-metadata
{chr(10).join(metadata_lines)}
-->"""
    return html_desc


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_tag(tags_str: str, pattern: str) -> str:
    """Extract first matching tag from semicolon-delimited ADO tag string."""
    for tag in tags_str.split(";"):
        tag = tag.strip().lower()
        if re.search(pattern, tag):
            return tag
    return ""
