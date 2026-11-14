"""Beat task driving the Bedrock KB ingestion queue (``services/bedrock_sync``).

Bedrock allows one ingestion job per data source, so the tick must have a
single runner: it takes a session-level Postgres advisory lock on a dedicated
connection (``pg_try_advisory_lock``) and skips when another tick holds it.
The work session commits several times, which would release a transaction
lock; the session lock lives until the explicit unlock or the connection
closes (worker death), so a crashed tick never wedges the queue.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from celery import current_app
from sqlalchemy import text

from src.services.bedrock_sync import (
    bedrock_sync_enabled,
    get_bedrock_agent_client,
    run_sync_tick,
)
from src.tasks._async_utils import run_async
from src.tasks.celery_app import celery_app  # noqa: F401 - binds tasks to the app

logger = logging.getLogger(__name__)

_LOCK_NAMESPACE = 0x42444B42  # "BDKB"
_LOCK_KEY = 1


@asynccontextmanager
async def tick_lock(connection: Any) -> AsyncIterator[bool]:
    """Yield whether this process holds the single-runner lock."""
    result = await connection.execute(
        text("SELECT pg_try_advisory_lock(:ns, :key)"),
        {"ns": _LOCK_NAMESPACE, "key": _LOCK_KEY},
    )
    acquired = bool(result.scalar())
    await connection.commit()
    try:
        yield acquired
    finally:
        if acquired:
            await connection.execute(
                text("SELECT pg_advisory_unlock(:ns, :key)"),
                {"ns": _LOCK_NAMESPACE, "key": _LOCK_KEY},
            )
            await connection.commit()


async def run_locked_tick(connection: Any, session_factory: Any, client: Any) -> dict:
    async with tick_lock(connection) as acquired:
        if not acquired:
            return {"skipped": "locked"}
        async with session_factory() as db:
            try:
                return await run_sync_tick(db, client)
            except Exception:
                await db.rollback()
                raise


async def _tick() -> dict:
    from src.core.database import AsyncSessionLocal, async_engine

    async with async_engine.connect() as connection:
        return await run_locked_tick(
            connection, AsyncSessionLocal, get_bedrock_agent_client()
        )


@current_app.task(name="src.tasks.bedrock_sync_tasks.bedrock_kb_sync_tick")
def bedrock_kb_sync_tick() -> dict:
    """Settle the running ingestion job or start the next one."""
    if not bedrock_sync_enabled():
        return {"skipped": "disabled"}
    summary = run_async(_tick())
    logger.info("bedrock_kb sync tick: %s", summary)
    return summary


current_app.conf.beat_schedule.update(
    {
        "bedrock-kb-sync-tick": {
            "task": "src.tasks.bedrock_sync_tasks.bedrock_kb_sync_tick",
            "schedule": 60.0,
        },
    }
)
