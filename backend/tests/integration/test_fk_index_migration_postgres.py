"""fk01_purge_path_fk_indexes on PostgreSQL, starting without the indexes.

A from-empty ``alembic upgrade head`` never exercises this revision's CREATE
path, because the ``r6h3`` baseline's ``create_all`` already builds every
model index. These tests build a schema that has the purge-path tables but
none of the 15 indexes, as a live database had, and run the revision on it.

Requires ORCHESTRATION_TEST_DATABASE_URL (skips otherwise: NOT RUN).
Mutation checks: skipping the INVALID-index drop fails
test_upgrade_repairs_an_invalid_index; removing ``SET lock_timeout`` makes
test_a_blocked_build_fails_fast_and_a_rerun_repairs_it hang until its
asyncio timeout instead of failing fast.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/fk01_purge_path_fk_indexes.py"
)


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("fk01_test", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tables(columns: Any) -> Dict[str, List[str]]:
    tables: Dict[str, List[str]] = {}
    for table, column in columns:
        tables.setdefault(table, []).append(column)
    return tables


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    url = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not url:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = "fk01_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with scoped.begin() as conn:
            for table, columns in _tables(_migration().FK_COLUMNS).items():
                cols = ", ".join(f"{c} UUID" for c in columns)
                await conn.execute(
                    text(f"CREATE TABLE {table} (id UUID PRIMARY KEY, {cols})")
                )
                await conn.execute(
                    text(
                        f"INSERT INTO {table} "
                        f"SELECT gen_random_uuid() FROM generate_series(1, 50)"
                    )
                )
        yield scoped
    finally:
        await scoped.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


async def _run(engine: AsyncEngine, step: str, **overrides: Any) -> None:
    migration = _migration()
    for name, value in overrides.items():
        assert hasattr(migration, name), name
        setattr(migration, name, value)

    def migrate(connection: Any) -> None:
        context = MigrationContext.configure(connection)
        with Operations.context(context), context.begin_transaction():
            getattr(migration, step)()

    async with engine.connect() as conn:
        await conn.run_sync(migrate)


async def _indexes(engine: AsyncEngine) -> Dict[str, bool]:
    async with engine.connect() as db:
        rows = await db.execute(
            text(
                "SELECT c.relname, i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relname LIKE 'ix_%'"
            )
        )
        return {name: valid for name, valid in rows}


def _expected() -> Dict[str, bool]:
    migration = _migration()
    return {migration._name(t, c): True for t, c in migration.FK_COLUMNS}


async def _lock_timeout(engine: AsyncEngine) -> str:
    async with engine.connect() as db:
        return str(await db.scalar(text("SHOW lock_timeout")))


async def test_upgrade_builds_every_index_on_a_schema_without_them(
    engine: AsyncEngine,
) -> None:
    assert await _indexes(engine) == {}

    await _run(engine, "upgrade")
    assert await _indexes(engine) == _expected()
    assert len(_expected()) == 15

    await _run(engine, "upgrade")  # idempotent re-run
    assert await _indexes(engine) == _expected()


async def test_the_index_serves_the_fk_lookup(engine: AsyncEngine) -> None:
    await _run(engine, "upgrade")
    async with engine.begin() as db:
        await db.execute(text("ANALYZE agent_runs"))
        await db.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            row[0]
            for row in await db.execute(
                text(
                    "EXPLAIN SELECT 1 FROM agent_runs "
                    "WHERE thread_id = gen_random_uuid()"
                )
            )
        )
    assert "ix_agent_runs_thread_id" in plan


async def test_upgrade_repairs_an_invalid_index(engine: AsyncEngine) -> None:
    await _run(engine, "upgrade")
    async with engine.begin() as db:
        await db.execute(
            text(
                "UPDATE pg_index SET indisvalid = false "
                "WHERE indexrelid = to_regclass('ix_chat_messages_parent_message_id')"
            )
        )
    assert (await _indexes(engine))["ix_chat_messages_parent_message_id"] is False

    await _run(engine, "upgrade")
    assert await _indexes(engine) == _expected()


async def test_a_blocked_build_fails_fast_and_a_rerun_repairs_it(
    engine: AsyncEngine,
) -> None:
    # An open writer on agent_runs (here: idle in transaction after an
    # INSERT) blocks CREATE INDEX CONCURRENTLY on that table.
    async with engine.connect() as blocker:
        await blocker.execute(
            text("INSERT INTO agent_runs (id) VALUES (gen_random_uuid())")
        )
        with pytest.raises(RuntimeError, match=r"ix_agent_runs_thread_id: gave up"):
            await asyncio.wait_for(
                _run(engine, "upgrade", _LOCK_TIMEOUT="200ms"), timeout=30
            )
        await blocker.rollback()

    assert await _lock_timeout(engine) == "0"  # RESET on the way out
    assert (await _indexes(engine)).get("ix_agent_runs_thread_id") is False

    await _run(engine, "upgrade")
    assert await _indexes(engine) == _expected()


async def test_downgrade_drops_every_index(engine: AsyncEngine) -> None:
    await _run(engine, "upgrade")
    await _run(engine, "downgrade")
    assert await _indexes(engine) == {}
