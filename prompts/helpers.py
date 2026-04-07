"""Shared utilities used by prompt builders."""

from __future__ import annotations

import re

_TABLE_METADATA_KEYS = {
    "environment": "environment",
    "workspace": "workspace_url",
    "job id": "job_id",
    "run id": "run_id",
    "failed task": "failed_task",
    "target table": "target_table",
    "source": "source",
    "alert rule": "alert_rule",
    "incident target": "incident_target",
    "target resource id": "target_resource_id",
    "target resource name": "target_resource_name",
    "target resource group": "target_resource_group",
    "target resource type": "target_resource_type",
    "target subscription": "target_subscription",
}


def _parse_html_table_metadata(html_desc: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    row_pattern = re.compile(
        r"<tr>\s*<td>\s*<b>(.*?)</b>\s*</td>\s*<td>(.*?)</td>\s*</tr>",
        re.IGNORECASE | re.DOTALL,
    )
    for label, value in row_pattern.findall(html_desc):
        key = _TABLE_METADATA_KEYS.get(strip_html(label).strip().lower())
        if not key:
            continue
        cleaned = strip_html(value).strip()
        if cleaned:
            meta[key] = cleaned
    return meta


def parse_agent_metadata(html_desc: str) -> dict[str, str]:
    """Extract machine-readable metadata from the HTML comment block in the description."""
    meta: dict[str, str] = {}
    match = re.search(r"<!--\s*agent-metadata(.*?)-->", html_desc, re.DOTALL)
    if match:
        for line in match.group(1).strip().splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                meta[key.strip()] = val.strip()
    for key, value in _parse_html_table_metadata(html_desc).items():
        meta.setdefault(key, value)
    return meta


def strip_html(html: str) -> str:
    """Very light HTML stripping - keep it readable for the LLM."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()
