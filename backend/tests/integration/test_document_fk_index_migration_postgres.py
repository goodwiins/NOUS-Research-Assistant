"""fk02_document_delete_fk_indexes on PostgreSQL, starting without the indexes.

A from-empty ``alembic upgrade head`` never exercises this revision's CREATE
path, because the ``r6h3`` baseline's ``create_all`` already builds every
model index. These tests build a schema that has the tables but none of the
indexes, as a live database has, and run the revision on it. The lock-wait
and terminated-build paths are the same code as fk01's and are covered by
``test_fk_index_migration_postgres.py``.

Requires ORCHESTRATION_TEST_DATABASE_URL (skips otherwise: NOT RUN).
"""

from __future__ import annotations

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
    / "alembic/versions/fk02_document_delete_fk_indexes.py"
)


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("fk02_test", _PATH)
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
    schema = "fk02_" + uuid4().hex
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


async def _invalidate(engine: AsyncEngine, name: str) -> None:
    async with engine.begin() as db:
        await db.execute(
            text(
                "UPDATE pg_index SET indisvalid = false "
                "WHERE indexrelid = to_regclass(:name)"
            ),
            {"name": name},
        )


async def test_upgrade_builds_every_index_on_a_schema_without_them(
    engine: AsyncEngine,
) -> None:
    assert await _indexes(engine) == {}

    await _run(engine, "upgrade")
    assert await _indexes(engine) == _expected()
    assert len(_expected()) == 8

    await _run(engine, "upgrade")  # idempotent re-run
    assert await _indexes(engine) == _expected()


async def test_the_index_serves_the_fk_lookup(engine: AsyncEngine) -> None:
    await _run(engine, "upgrade")
    async with engine.begin() as db:
        await db.execute(text("ANALYZE entities"))
        await db.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            row[0]
            for row in await db.execute(
                text(
                    "EXPLAIN SELECT 1 FROM entities "
                    "WHERE document_id = gen_random_uuid()"
                )
            )
        )
    assert "ix_entities_document_id" in plan


async def test_upgrade_repairs_an_invalid_index(engine: AsyncEngine) -> None:
    await _run(engine, "upgrade")
    await _invalidate(engine, "ix_search_results_document_id")
    assert (await _indexes(engine))["ix_search_results_document_id"] is False

    await _run(engine, "upgrade")
    assert await _indexes(engine) == _expected()


async def test_upgrade_refuses_to_finish_over_an_invalid_index(
    engine: AsyncEngine,
) -> None:
    # Without the repair, IF NOT EXISTS accepts the INVALID index; the
    # validity check must then fail the upgrade instead of letting it stamp.
    await _run(engine, "upgrade")
    await _invalidate(engine, "ix_threads_branch_document_id")

    with pytest.raises(Exception, match="Expected valid index"):
        await _run(engine, "upgrade", _is_invalid=lambda name: False)

    await _run(engine, "upgrade")
    assert await _indexes(engine) == _expected()


async def test_downgrade_drops_every_index(engine: AsyncEngine) -> None:
    await _run(engine, "upgrade")
    await _run(engine, "downgrade")
    assert await _indexes(engine) == {}
