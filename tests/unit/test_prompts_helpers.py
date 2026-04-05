from __future__ import annotations

import re

from integrations.ado import build_bug_description
from prompts.helpers import parse_agent_metadata


def test_parse_agent_metadata_falls_back_to_visible_table_rows() -> None:
    html = build_bug_description(
        environment="azure",
        workspace_url="/subscriptions/test-sub/providers/Microsoft.AlertsManagement/alerts/abc",
        job_id="alert-rule-1",
        run_id="2026-04-05T12:00:00Z",
        failed_task="aca-worker-live-smoke",
        error_message="timed out",
        failure_type="infra",
        alert_rule="alert-rule-1",
        incident_target="aca-worker-live-smoke",
        target_resource_name="aca-worker-live-smoke",
    )
    without_comment = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)

    meta = parse_agent_metadata(without_comment)

    assert meta["environment"] == "azure"
    assert meta["alert_rule"] == "alert-rule-1"
    assert meta["incident_target"] == "aca-worker-live-smoke"
    assert meta["target_resource_name"] == "aca-worker-live-smoke"
