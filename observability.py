"""
MLflow runtime tracing helpers.

Tracing must never break the agent. All helper operations degrade to no-op on
MLflow failures while preserving the caller's normal control flow.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from typing import Any

import mlflow
import structlog

log = structlog.get_logger()
_EXPERIMENT_LOCK = Lock()
_EXPERIMENT_READY = False
_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_HINTS = (
    "token",
    "secret",
    "password",
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "connectionstring",
)


def tracing_enabled() -> bool:
    return os.getenv("MLFLOW_RUNTIME_TRACING_ENABLED", "true").lower() not in {
        "0",
        "false",
        "no",
    }


def ensure_trace_destination() -> None:
    global _EXPERIMENT_READY

    if _EXPERIMENT_READY or not tracing_enabled():
        return

    with _EXPERIMENT_LOCK:
        if _EXPERIMENT_READY or not tracing_enabled():
            return
        try:
            mlflow.set_experiment(
                os.getenv("MLFLOW_TRACING_EXPERIMENT_NAME", "pipeline-resolver-runtime")
            )
            _EXPERIMENT_READY = True
        except Exception as exc:
            log.warning("tracing.set_experiment_failed", error=str(exc))


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", key.lower())


def _is_sensitive_key(key: str | None) -> bool:
    if not key:
        return False
    normalized = _normalized_key(key)
    return any(hint in normalized for hint in _SENSITIVE_KEY_HINTS)


def compact_value(
    value: Any,
    *,
    depth: int = 0,
    max_items: int = 10,
    max_text: int = 500,
    key_hint: str | None = None,
) -> Any:
    if _is_sensitive_key(key_hint):
        return _REDACTED
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        if len(value) <= max_text:
            return value
        return f"{value[: max_text - 3]}..."
    if depth >= 3:
        return compact_value(repr(value), max_text=max_text)
    if isinstance(value, dict):
        items = list(value.items())[:max_items]
        return {
            str(key): compact_value(
                item,
                depth=depth + 1,
                max_items=max_items,
                max_text=max_text,
                key_hint=str(key),
            )
            for key, item in items
        }
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:max_items]
        return [
            compact_value(item, depth=depth + 1, max_items=max_items, max_text=max_text)
            for item in items
        ]
    return compact_value(repr(value), max_text=max_text)


def stringify_mapping(values: dict[str, Any] | None) -> dict[str, str] | None:
    if not values:
        return None
    return {str(key): str(compact_value(value)) for key, value in values.items()}


@contextmanager
def start_trace_span(
    name: str,
    *,
    span_type: str = "UNKNOWN",
    attributes: dict[str, Any] | None = None,
    inputs: Any = None,
) -> Iterator[Any | None]:
    if not tracing_enabled():
        yield None
        return

    span_cm = None
    span = None
    try:
        ensure_trace_destination()
        span_cm = mlflow.start_span(
            name=name,
            span_type=span_type,
            attributes=compact_value(attributes) if attributes else None,
        )
        span = span_cm.__enter__()
        if inputs is not None:
            span.set_inputs(compact_value(inputs))
    except Exception as exc:
        log.warning("tracing.start_span_failed", span=name, error=str(exc))
        yield None
        return

    try:
        yield span
    finally:
        try:
            span_cm.__exit__(*sys.exc_info())
        except Exception as exc:
            log.warning("tracing.finish_span_failed", span=name, error=str(exc))


def set_span_outputs(span: Any | None, outputs: Any) -> None:
    if span is None or not tracing_enabled():
        return
    try:
        span.set_outputs(compact_value(outputs))
    except Exception as exc:
        log.warning("tracing.set_outputs_failed", error=str(exc))


def set_span_inputs(span: Any | None, inputs: Any) -> None:
    if span is None or not tracing_enabled():
        return
    try:
        span.set_inputs(compact_value(inputs))
    except Exception as exc:
        log.warning("tracing.set_inputs_failed", error=str(exc))


def set_span_attributes(span: Any | None, attributes: dict[str, Any]) -> None:
    if span is None or not tracing_enabled():
        return
    try:
        span.set_attributes(compact_value(attributes))
    except Exception as exc:
        log.warning("tracing.set_attributes_failed", error=str(exc))


def set_span_status(span: Any | None, status: str) -> None:
    if span is None or not tracing_enabled():
        return
    try:
        span.set_status(status)
    except Exception as exc:
        log.warning("tracing.set_status_failed", error=str(exc))


def update_trace(
    *,
    tags: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    client_request_id: str | None = None,
    request_preview: str | None = None,
    response_preview: str | None = None,
    state: str | None = None,
) -> None:
    if not tracing_enabled():
        return
    try:
        mlflow.update_current_trace(
            tags=stringify_mapping(tags),
            metadata=stringify_mapping(metadata),
            client_request_id=client_request_id,
            request_preview=str(compact_value(request_preview)) if request_preview else None,
            response_preview=str(compact_value(response_preview)) if response_preview else None,
            state=state,
        )
    except Exception as exc:
        log.warning("tracing.update_trace_failed", error=str(exc))
