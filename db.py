"""
Database layer.

Two kinds of state live in Neon:
  1. agent_state.* tables - our coordination, dedup, and webhook idempotency state
  2. public.*            - LangGraph AsyncPostgresSaver checkpoint tables

The coordination schema is bootstrapped independently from LangGraph so that the
Glue Function and Intake API can use the database before any worker starts.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from config import settings

log = structlog.get_logger()


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    INVESTIGATING = "INVESTIGATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"


RunRecord = dict[str, Any]


_pool: AsyncConnectionPool | None = None
_schema_bootstrapped = False
_schema_lock = asyncio.Lock()


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            open=False,
            reconnect_attempts=3,
            reconnect_timeout=5.0,
            kwargs={"sslmode": "require"},
        )
        await _pool.open()
        log.info("db.pool_opened", max_size=settings.db_pool_max_size)
    return _pool


async def close_pool() -> None:
    global _pool, _schema_bootstrapped
    if _pool:
        await _pool.close()
        _pool = None
        _schema_bootstrapped = False
        log.info("db.pool_closed")


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS agent_state;

CREATE TABLE IF NOT EXISTS agent_state.agent_runs (
    run_id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    work_item_id        INTEGER     NOT NULL UNIQUE,
    work_item_url       TEXT,
    thread_id           TEXT        NOT NULL UNIQUE,
    attempt_count       INTEGER     NOT NULL DEFAULT 0,
    status              TEXT        NOT NULL DEFAULT 'QUEUED',
    target_env          TEXT,
    failure_type        TEXT,
    proposed_fix_url    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT agent_runs_attempt_non_negative CHECK (attempt_count >= 0)
);

CREATE TABLE IF NOT EXISTS agent_state.processed_events (
    event_id            TEXT        PRIMARY KEY,
    event_type          TEXT        NOT NULL,
    work_item_id        INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_state.alert_dedup (
    dedup_key           TEXT        PRIMARY KEY,
    work_item_id        INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_status
    ON agent_state.agent_runs (status)
    WHERE status NOT IN ('RESOLVED', 'ESCALATED');

CREATE INDEX IF NOT EXISTS idx_alert_dedup_last_seen
    ON agent_state.alert_dedup (last_seen_at DESC);

CREATE OR REPLACE FUNCTION agent_state.set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_agent_runs_updated_at ON agent_state.agent_runs;
CREATE TRIGGER trg_agent_runs_updated_at
    BEFORE UPDATE ON agent_state.agent_runs
    FOR EACH ROW EXECUTE FUNCTION agent_state.set_updated_at();
"""

CLEANUP_SQL = """
DELETE FROM checkpoints
WHERE thread_id IN (
    SELECT thread_id
    FROM agent_state.agent_runs
    WHERE updated_at < NOW() - INTERVAL '{days} days'
      AND status IN ('RESOLVED', 'ESCALATED')
);

DELETE FROM agent_state.processed_events
WHERE created_at < NOW() - INTERVAL '{days} days';

DELETE FROM agent_state.alert_dedup
WHERE last_seen_at < NOW() - INTERVAL '{days} days';

DELETE FROM agent_state.agent_runs
WHERE updated_at < NOW() - INTERVAL '{days} days'
  AND status IN ('RESOLVED', 'ESCALATED');
"""


async def bootstrap_db() -> None:
    """Create the coordination schema exactly once per process."""
    global _schema_bootstrapped
    if _schema_bootstrapped:
        return

    async with _schema_lock:
        if _schema_bootstrapped:
            return
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(SCHEMA_SQL)
            await conn.commit()
        _schema_bootstrapped = True
        log.info("db.schema_bootstrap_complete")


async def bootstrap(checkpointer: AsyncPostgresSaver) -> None:
    """Create our schema and LangGraph's checkpoint tables."""
    await bootstrap_db()
    await checkpointer.setup()
    log.info("db.checkpointer_bootstrap_complete")


@asynccontextmanager
async def get_checkpointer() -> AsyncGenerator[AsyncPostgresSaver, None]:
    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as checkpointer:
        yield checkpointer


def make_thread_id(work_item_id: int, attempt: int) -> str:
    return f"work_item_{work_item_id}_attempt_{attempt}"


async def _fetchone_dict(conn, query: str, params: tuple[Any, ...]) -> RunRecord | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        row = await cur.fetchone()
    return dict(row) if row else None


async def record_processed_event(
    event_id: str,
    event_type: str,
    work_item_id: int | None = None,
) -> bool:
    """
    Record a webhook event exactly once.

    Returns True when the caller should process the event, False when it has
    already been seen.
    """
    await bootstrap_db()
    pool = await get_pool()
    async with pool.connection() as conn:
        row = await _fetchone_dict(
            conn,
            """
            INSERT INTO agent_state.processed_events (event_id, event_type, work_item_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """,
            (event_id, event_type, work_item_id),
        )
        await conn.commit()
    return row is not None


async def claim_alert_dedup(dedup_key: str, window_minutes: int = 5) -> bool:
    """
    Atomically claim an alert dedup slot.

    Returns True when the caller should create a new work item. Returns False when
    another alert with the same key has already been seen within the dedup window.
    """
    await bootstrap_db()
    cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)
    pool = await get_pool()
    async with pool.connection() as conn:
        row = await _fetchone_dict(
            conn,
            """
            INSERT INTO agent_state.alert_dedup (dedup_key, last_seen_at)
            VALUES (%s, NOW())
            ON CONFLICT (dedup_key) DO UPDATE
            SET last_seen_at = EXCLUDED.last_seen_at
            WHERE agent_state.alert_dedup.last_seen_at < %s
            RETURNING dedup_key
            """,
            (dedup_key, cutoff),
        )
        await conn.commit()
    return row is not None


async def attach_alert_work_item(dedup_key: str, work_item_id: int) -> None:
    await bootstrap_db()
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """
            UPDATE agent_state.alert_dedup
            SET work_item_id = %s,
                last_seen_at = NOW()
            WHERE dedup_key = %s
            """,
            (work_item_id, dedup_key),
        )
        await conn.commit()


async def ensure_run(
    work_item_id: int,
    work_item_url: str = "",
    *,
    target_env: str | None = None,
    failure_type: str | None = None,
) -> RunRecord:
    """Create the run record once and return the current row."""
    await bootstrap_db()
    pool = await get_pool()
    async with pool.connection() as conn:
        created = await _fetchone_dict(
            conn,
            """
            INSERT INTO agent_state.agent_runs
                (
                    run_id,
                    work_item_id,
                    work_item_url,
                    thread_id,
                    attempt_count,
                    status,
                    target_env,
                    failure_type
                )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (work_item_id) DO NOTHING
            RETURNING *
            """,
            (
                str(uuid.uuid4()),
                work_item_id,
                work_item_url,
                make_thread_id(work_item_id, 0),
                0,
                RunStatus.QUEUED.value,
                target_env or None,
                failure_type or None,
            ),
        )
        if created is None and any((work_item_url, target_env, failure_type)):
            await conn.execute(
                """
                UPDATE agent_state.agent_runs
                SET work_item_url = COALESCE(NULLIF(%s, ''), work_item_url)
                  , target_env = COALESCE(NULLIF(%s, ''), target_env)
                  , failure_type = COALESCE(NULLIF(%s, ''), failure_type)
                WHERE work_item_id = %s
                """,
                (
                    work_item_url,
                    target_env or "",
                    failure_type or "",
                    work_item_id,
                ),
            )
        row = await _fetchone_dict(
            conn,
            "SELECT * FROM agent_state.agent_runs WHERE work_item_id = %s",
            (work_item_id,),
        )
        await conn.commit()

    if row is None:
        msg = f"Failed to ensure run for work_item_id={work_item_id}"
        raise RuntimeError(msg)
    return row


async def queue_retry(work_item_id: int) -> RunRecord | None:
    """
    Advance a run to the next attempt atomically.

    Returns None when the work item has exhausted its configured attempt budget or
    is already escalated.
    """
    await bootstrap_db()
    pool = await get_pool()
    async with pool.connection() as conn:
        row = await _fetchone_dict(
            conn,
            """
            UPDATE agent_state.agent_runs
            SET attempt_count = attempt_count + 1,
                thread_id = CONCAT('work_item_', work_item_id, '_attempt_', attempt_count + 1),
                status = %s
            WHERE work_item_id = %s
              AND status != %s
              AND attempt_count < %s
            RETURNING *
            """,
            (
                RunStatus.QUEUED.value,
                work_item_id,
                RunStatus.ESCALATED.value,
                settings.max_attempts,
            ),
        )
        await conn.commit()
    return row


async def begin_approval_resume(work_item_id: int) -> RunRecord | None:
    """Move a paused run back into INVESTIGATING so it can resume exactly once."""
    await bootstrap_db()
    pool = await get_pool()
    async with pool.connection() as conn:
        row = await _fetchone_dict(
            conn,
            """
            UPDATE agent_state.agent_runs
            SET status = %s
            WHERE work_item_id = %s
              AND status = %s
            RETURNING *
            """,
            (
                RunStatus.INVESTIGATING.value,
                work_item_id,
                RunStatus.AWAITING_APPROVAL.value,
            ),
        )
        await conn.commit()
    return row


async def update_status(
    work_item_id: int,
    status: RunStatus,
    proposed_fix_url: str | None = None,
) -> None:
    await bootstrap_db()
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """
            UPDATE agent_state.agent_runs
            SET status = %s,
                proposed_fix_url = COALESCE(%s, proposed_fix_url)
            WHERE work_item_id = %s
            """,
            (status.value, proposed_fix_url, work_item_id),
        )
        await conn.commit()


async def get_run(work_item_id: int) -> RunRecord | None:
    await bootstrap_db()
    pool = await get_pool()
    async with pool.connection() as conn:
        return await _fetchone_dict(
            conn,
            "SELECT * FROM agent_state.agent_runs WHERE work_item_id = %s",
            (work_item_id,),
        )
