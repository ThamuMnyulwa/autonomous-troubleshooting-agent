"""
Azure Service Bus queue integration.

The intake API publishes run envelopes to a single queue. The worker can either:
  - receive a pre-baked payload via JOB_PAYLOAD, or
  - pull one message directly from Service Bus and process it before exiting.

This fits Azure Container Apps Jobs well: each job instance can drain one queue
message, execute the LangGraph run, then terminate.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

import structlog
from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import ServiceBusMessage, ServiceBusReceiveMode
from azure.servicebus.aio import AutoLockRenewer, ServiceBusClient

from config import settings

log = structlog.get_logger()

RunAction = Literal["investigate", "resume"]
RunMessage = dict[str, Any]
RunHandler = Callable[[RunMessage], Awaitable[None]]


def is_queue_configured() -> bool:
    return bool(
        settings.azure_service_bus_queue_name
        and (
            settings.azure_service_bus_connection_string
            or settings.azure_service_bus_namespace
        )
    )


def normalize_run_message(payload: dict[str, Any]) -> RunMessage:
    action = payload.get("action", "investigate")
    if action not in {"investigate", "resume"}:
        msg = f"Unsupported queue action: {action!r}"
        raise ValueError(msg)

    return {
        "work_item_id": int(payload["work_item_id"]),
        "run_id": str(payload.get("run_id", "")),
        "attempt": int(payload.get("attempt", 0)),
        "thread_id": str(payload.get("thread_id", "")),
        "action": action,
    }


def build_run_message(run: dict[str, Any], *, action: RunAction) -> RunMessage:
    return normalize_run_message(
        {
            "work_item_id": run["work_item_id"],
            "run_id": str(run["run_id"]),
            "attempt": run["attempt_count"],
            "thread_id": run["thread_id"],
            "action": action,
        }
    )


@asynccontextmanager
async def _service_bus_client():
    credential = None
    if settings.azure_service_bus_connection_string:
        client = ServiceBusClient.from_connection_string(
            settings.azure_service_bus_connection_string,
            logging_enable=False,
        )
    elif settings.azure_service_bus_namespace:
        credential = DefaultAzureCredential()
        client = ServiceBusClient(
            fully_qualified_namespace=settings.azure_service_bus_namespace,
            credential=credential,
            logging_enable=False,
        )
    else:
        msg = (
            "Azure Service Bus is not configured. Set AZURE_SERVICE_BUS_CONNECTION_STRING "
            "or AZURE_SERVICE_BUS_NAMESPACE."
        )
        raise RuntimeError(msg)

    try:
        async with client:
            yield client
    finally:
        if credential is not None:
            await credential.close()


async def publish_run_message(payload: RunMessage) -> None:
    message_id = f"{payload['work_item_id']}:{payload['attempt']}:{payload['action']}"
    body = json.dumps(payload)

    async with _service_bus_client() as client:
        async with client.get_queue_sender(
            queue_name=settings.azure_service_bus_queue_name,
        ) as sender:
            await sender.send_messages(
                ServiceBusMessage(
                    body,
                    content_type="application/json",
                    message_id=message_id,
                    subject=payload["action"],
                )
            )
    log.info(
        "queue.message_published",
        queue=settings.azure_service_bus_queue_name,
        work_item_id=payload["work_item_id"],
        action=payload["action"],
    )


async def process_next_run_message(handler: RunHandler) -> bool:
    """
    Pull one message from the queue, process it, and settle it.

    Returns False when no message was available within the configured wait time.
    """
    async with _service_bus_client() as client:
        async with AutoLockRenewer() as renewer:
            async with client.get_queue_receiver(
                queue_name=settings.azure_service_bus_queue_name,
                receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
                prefetch_count=1,
                max_wait_time=settings.azure_service_bus_receive_wait_seconds,
            ) as receiver:
                messages = await receiver.receive_messages(
                    max_message_count=1,
                    max_wait_time=settings.azure_service_bus_receive_wait_seconds,
                )
                if not messages:
                    return False

                message = messages[0]
                renewer.register(
                    receiver,
                    message,
                    max_lock_renewal_duration=settings.azure_service_bus_lock_renewal_seconds,
                )
                try:
                    payload = normalize_run_message(
                        json.loads(_message_body_text(message))
                    )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    log.error(
                        "queue.invalid_message",
                        error=str(exc),
                        message_id=message.message_id,
                    )
                    await receiver.dead_letter_message(
                        message,
                        reason="invalid_payload",
                        error_description=str(exc),
                    )
                    return True

                try:
                    await handler(payload)
                except Exception:
                    log.exception(
                        "queue.message_handler_failed",
                        message_id=message.message_id,
                        work_item_id=payload.get("work_item_id"),
                    )
                    await receiver.abandon_message(message)
                    raise

                await receiver.complete_message(message)
                log.info(
                    "queue.message_completed",
                    message_id=message.message_id,
                    work_item_id=payload["work_item_id"],
                    action=payload["action"],
                )
                return True


def _message_body_text(message) -> str:
    body = message.body
    if isinstance(body, str):
        return body
    if isinstance(body, (bytes, bytearray, memoryview)):
        return bytes(body).decode("utf-8")

    parts: list[bytes] = []
    for chunk in body:
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            parts.append(bytes(chunk))
        else:
            parts.append(str(chunk).encode("utf-8"))
    return b"".join(parts).decode("utf-8")
