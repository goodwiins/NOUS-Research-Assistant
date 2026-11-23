"""GOO-686 / GOO-690: ``ws01_websocket_status_tables`` on a real PostgreSQL.

The SQLite and offline-render tests live in
``test_websocket_status_tables_migration.py``. These need PostgreSQL: RLS,
PostgREST grants, native enum types and the ``DO``-block guards.

Each test creates and drops its own database on an admin server URL taken
from ``ORCHESTRATION_TEST_DATABASE_URL`` (the CI PostgreSQL lane sets it) or
``GOO686_PG_URL`` (a local scratch server). The file is named ``*_postgres.py``
so the CI lane runs it (GOO-702); there, ``REQUIRE_POSTGRES_TESTS=1`` turns a
skip into a failure. Outside that lane, without a URL, it skips.

Covered:

* ``alembic upgrade head`` from an EMPTY database ends with all four tables,
  RLS on and no PostgREST grants;
* the backfill path: missing tables are created, and existing exposed ones
  are locked down; a rerun is a no-op;
* online, ws01 creates only its own tables and enum types. Another missing
  enum type the metadata knows stays missing (``metadata.create_all`` would
  recreate it);
* the fail-loud guards refuse, before any DDL, an existing table that lacks
  a model column (the legacy Supabase ``websocket_connections``, and a
  sibling table), a same-named enum with other labels, and a same-named
  non-enum type.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT = "rl01_secure_late_internal_rls"
REVISION = "ws01_websocket_status_tables"
TABLES = (
    "websocket_connections",
    "connection_events",
    "status_updates",
    "notification_templates",
)

_PG_URL = os.environ.get("ORCHESTRATION_TEST_DATABASE_URL") or os.environ.get(
    "GOO686_PG_URL"
)


@pytest.fixture
def scratch_db() -> Iterator[str]:
    """A brand-new empty database on the admin server; dropped after."""
    if not _PG_URL:
        pytest.skip(
            "needs a PostgreSQL admin URL in ORCHESTRATION_TEST_DATABASE_URL "
            "or GOO686_PG_URL"
        )
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    admin = make_url(_PG_URL)
    if not admin.drivername.startswith("postgresql"):
        raise ValueError("the scratch server URL must be a PostgreSQL URL")
    name = f"goo686_{uuid.uuid4().hex[:12]}"
    engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    try:
        yield admin.set(database=name).render_as_string(hide_password=False)
    finally:
        with engine.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        engine.dispose()


def _child_env(url: str) -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(BACKEND_ROOT), "DATABASE_URL": url}


def _pg(url: str, sql: str) -> list:
    """Run ``;;``-separated ``sql`` on ``url``; return the last statement's rows.

    In a child process, like the upgrades, so this process's SQLAlchemy and
    alembic state never matter.
    """
    code = (
        "import json, os, sqlalchemy as sa\n"
        "e = sa.create_engine(os.environ['DATABASE_URL'])\n"
        "rows = []\n"
        "with e.begin() as c:\n"
        f"    for stmt in {sql!r}.split(';;'):\n"
        "        r = c.exec_driver_sql(stmt)\n"
        "        rows = [list(x) for x in r] if r.returns_rows else []\n"
        "print(json.dumps(rows, default=str))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_ROOT,
        env=_child_env(url),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    rows: list = json.loads(proc.stdout.strip().splitlines()[-1])
    return rows


def _upgrade(url: str, target: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=BACKEND_ROOT,
        env=_child_env(url),
        capture_output=True,
        text=True,
        timeout=900,
    )


def _at_parent(url: str) -> None:
    proc = _upgrade(url, PARENT)
    assert proc.returncode == 0, proc.stderr[-4000:]


def _regclass(url: str, table: str) -> object:
    [[found]] = _pg(url, f"SELECT to_regclass('public.{table}')")
    return found


def _regtype(url: str, type_name: str) -> object:
    [[found]] = _pg(url, f"SELECT to_regtype('public.{type_name}')")
    return found


_POSTGREST_ROLES_SQL = (
    "DO $$ BEGIN "
    "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') "
    "THEN CREATE ROLE anon NOLOGIN; END IF; "
    "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') "
    "THEN CREATE ROLE authenticated NOLOGIN; END IF; "
    "END $$;;"
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
    "GRANT ALL ON TABLES TO anon, authenticated"
)


def _assert_locked_down(url: str) -> None:
    for table in TABLES:
        [[rls]] = _pg(
            url,
            f"SELECT relrowsecurity FROM pg_class WHERE oid = 'public.{table}'::regclass",
        )
        assert rls is True, table
        for role in ("anon", "authenticated"):
            [[granted]] = _pg(
                url,
                f"SELECT has_table_privilege('{role}', 'public.{table}', 'SELECT')"
                f" OR has_table_privilege('{role}', 'public.{table}', 'INSERT')",
            )
            assert granted is False, (table, role)


def test_alembic_upgrade_head_from_empty_postgres_has_locked_down_tables(
    scratch_db: str,
) -> None:
    # Supabase-like: the PostgREST roles exist and get default grants.
    _pg(scratch_db, _POSTGREST_ROLES_SQL)
    proc = _upgrade(scratch_db, "head")
    assert proc.returncode == 0, proc.stderr[-4000:]
    _assert_locked_down(scratch_db)


def test_existing_exposed_tables_are_locked_down_and_missing_ones_created(
    scratch_db: str,
) -> None:
    """The backfill path: a DB past rl01 where some tables are missing and an
    existing one is world-readable (RLS off, PostgREST grants)."""
    _pg(scratch_db, _POSTGREST_ROLES_SQL)
    _at_parent(scratch_db)
    _pg(
        scratch_db,
        "DROP TABLE public.connection_events, public.status_updates CASCADE;;"
        "ALTER TABLE public.websocket_connections DISABLE ROW LEVEL SECURITY;;"
        "GRANT ALL ON public.websocket_connections TO anon, authenticated",
    )
    proc = _upgrade(scratch_db, REVISION)
    assert proc.returncode == 0, proc.stderr[-4000:]
    _assert_locked_down(scratch_db)
    # Idempotent on PostgreSQL too.
    assert _upgrade(scratch_db, REVISION).returncode == 0


def test_online_upgrade_creates_only_its_own_enum_types(scratch_db: str) -> None:
    """F1: ``Table.create`` per missing table, not ``metadata.create_all``.

    ``stanceenum`` belongs to an unrelated model (``stance_classifications``)
    and is on ``Base.metadata``. With it dropped, ``create_all(tables=...)``
    would recreate it while creating the ws01 tables; ws01 must not.
    """
    _at_parent(scratch_db)
    _pg(
        scratch_db,
        "DROP TABLE public.stance_classifications CASCADE;;"
        "DROP TYPE public.stanceenum;;"
        "DROP TABLE public.connection_events, public.status_updates, "
        "public.notification_templates, public.websocket_connections CASCADE;;"
        "DROP TYPE public.priority, public.updatetype, public.connectionstatus",
    )
    proc = _upgrade(scratch_db, REVISION)
    assert proc.returncode == 0, proc.stderr[-4000:]
    for table in TABLES:
        assert _regclass(scratch_db, table) == table, table
    for own in ("priority", "updatetype", "connectionstatus"):
        assert _regtype(scratch_db, own) == own, own
    # The foreign enum stays missing.
    assert _regtype(scratch_db, "stanceenum") is None


def test_upgrade_refuses_the_legacy_supabase_websocket_connections_shape(
    scratch_db: str,
) -> None:
    _at_parent(scratch_db)
    _pg(
        scratch_db,
        "DROP TABLE public.connection_events, public.status_updates, "
        "public.websocket_connections CASCADE;;"
        "CREATE TABLE public.websocket_connections (id uuid PRIMARY KEY, "
        "connection_id varchar(255) NOT NULL, user_id uuid NOT NULL, client_ip inet)",
    )
    proc = _upgrade(scratch_db, REVISION)
    assert proc.returncode != 0
    assert "existing public.websocket_connections is missing column(s)" in proc.stderr
    # It says WHICH columns, and how to look.
    assert "organization_id" in proc.stderr and "connection_status" in proc.stderr
    assert "information_schema.columns" in proc.stderr
    # Nothing was created: the guard runs before any DDL.
    assert _regclass(scratch_db, "status_updates") is None


def test_upgrade_refuses_a_sibling_table_missing_a_model_column(
    scratch_db: str,
) -> None:
    """F2: the column guard covers all four tables, not just the legacy
    ``websocket_connections``. Here ``status_updates`` lacks ``was_clicked``."""
    _at_parent(scratch_db)
    _pg(
        scratch_db,
        "ALTER TABLE public.status_updates DROP COLUMN was_clicked;;"
        "DROP TABLE public.notification_templates CASCADE",
    )
    proc = _upgrade(scratch_db, REVISION)
    assert proc.returncode != 0
    assert "existing public.status_updates is missing column(s)" in proc.stderr
    assert "was_clicked" in proc.stderr
    assert "table_name = 'status_updates'" in proc.stderr
    # Nothing changed: no table created, the column not re-added, and the
    # revision not recorded.
    assert _regclass(scratch_db, "notification_templates") is None
    [[has_column]] = _pg(
        scratch_db,
        "SELECT count(*) FROM information_schema.columns WHERE table_schema = "
        "'public' AND table_name = 'status_updates' AND column_name = 'was_clicked'",
    )
    assert has_column == 0
    [[version]] = _pg(scratch_db, "SELECT version_num FROM alembic_version")
    assert version == PARENT


def test_upgrade_refuses_an_existing_enum_with_other_labels(scratch_db: str) -> None:
    _at_parent(scratch_db)
    _pg(
        scratch_db,
        "DROP TABLE public.status_updates, public.notification_templates CASCADE;;"
        "DROP TYPE public.priority;;"
        "CREATE TYPE public.priority AS ENUM ('low', 'high')",
    )
    proc = _upgrade(scratch_db, REVISION)
    assert proc.returncode != 0
    assert "existing enum type public.priority" in proc.stderr


def test_upgrade_refuses_a_same_named_non_enum_type(scratch_db: str) -> None:
    _at_parent(scratch_db)
    _pg(
        scratch_db,
        "DROP TABLE public.status_updates, public.notification_templates CASCADE;;"
        "DROP TYPE public.priority;;"
        "CREATE DOMAIN public.priority AS text",
    )
    proc = _upgrade(scratch_db, REVISION)
    assert proc.returncode != 0
    assert "public.priority exists but is not an enum type" in proc.stderr
    assert "<NULL>" not in proc.stderr
