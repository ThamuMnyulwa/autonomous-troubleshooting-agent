from __future__ import annotations

from observability import compact_value


def test_compact_value_redacts_sensitive_keys() -> None:
    payload = {
        "token": "plain-token",
        "nested": {
            "connection_string": "Endpoint=https://example/;SharedAccessKey=secret",
            "safe": "visible",
        },
        "items": [{"api_key": "abcdef"}, {"name": "worker"}],
    }

    assert compact_value(payload) == {
        "token": "[REDACTED]",
        "nested": {
            "connection_string": "[REDACTED]",
            "safe": "visible",
        },
        "items": [{"api_key": "[REDACTED]"}, {"name": "worker"}],
    }


def test_compact_value_truncates_large_values() -> None:
    payload = {"message": "x" * 600}

    compacted = compact_value(payload)

    assert compacted["message"].endswith("...")
    assert len(compacted["message"]) == 500
