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

import httpx
import structlog

from config import settings

log = structlog.get_logger()
_API = "7.1"


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
        failure_type = _extract_meta(desc, "failure_type")

        return {
            "id":           wi["id"],
            "url":          wi.get("url", ""),
            "fields":       fields,
            "target_env":   target_env or "databricks",
            "failure_type": failure_type or "unknown",
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
        """Update System.State field (New / Active / Resolved / Escalated)."""
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
        Create a new Bug work item from an incoming alert.
        Called by the alert intake path.
        """
        url = f"{self._base}/{self._project}/_apis/wit/workitems/$Bug"
        body = [
            {"op": "add", "path": "/fields/System.Title",       "value": title},
            {"op": "add", "path": "/fields/System.Description", "value": description_html},
            {"op": "add", "path": "/fields/System.Tags",        "value": tags},
            {"op": "add", "path": "/fields/System.State",       "value": "New"},
        ]
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
    escaped_error_message = html.escape(str(error_message))
    workspace_row = (
        f'<tr><td><b>Workspace</b></td><td><a href="{escaped_workspace_href}">'
        f"{escaped_workspace_text}</a></td></tr>"
    )

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
</table>

<h4>Error Message</h4>
<pre>{escaped_error_message}</pre>
</div>

<!-- agent-metadata
environment: {escaped_environment}
workspace_url: {escaped_workspace_text}
job_id: {escaped_job_id}
run_id: {escaped_run_id}
failed_task: {escaped_failed_task}
target_table: {escaped_target_table}
failure_type: {html.escape(str(failure_type))}
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


def _extract_meta(html: str, key: str) -> str:
    """Pull a value from the agent-metadata HTML comment block."""
    block = re.search(r"<!--\s*agent-metadata(.*?)-->", html, re.DOTALL)
    if not block:
        return ""
    match = re.search(rf"{re.escape(key)}:\s*(.+)", block.group(1))
    return match.group(1).strip() if match else ""
