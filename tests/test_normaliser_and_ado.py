from __future__ import annotations

from glue.normaliser import detect_source, make_alert_dedup_key
from integrations.ado import build_bug_description


def test_make_alert_dedup_key_is_stable() -> None:
    assert make_alert_dedup_key("job-123", "schema_mismatch") == make_alert_dedup_key(
        "job-123",
        "schema_mismatch",
    )


def test_build_bug_description_carries_failure_type_and_escapes_html() -> None:
    html = build_bug_description(
        environment="databricks",
        workspace_url='https://adb.example.com/?x=<script>',
        job_id="job-123",
        run_id="run-456",
        failed_task="<bad-task>",
        error_message="<boom>",
        failure_type="schema_mismatch",
    )

    assert "failure_type: schema_mismatch" in html
    assert "&lt;boom&gt;" in html
    assert "<script>" not in html


def test_detect_source_uses_headers_and_payload_shape() -> None:
    assert detect_source({"run": {}}, headers={}) == "databricks"
    assert detect_source(
        {"schemaId": "azureMonitorCommonAlertSchema"},
        headers={},
    ) == "azure"
