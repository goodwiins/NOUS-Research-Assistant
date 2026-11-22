"""GOO-686: the realtime/WebSocket status tables must exist on alembic-built DBs.

``src/models/websocket_status.py`` maps ``websocket_connections``,
``connection_events``, ``status_updates`` and ``notification_templates``. They
back mounted routes (``/api/v2/realtime/*``, ``/api/v2/ws/connect``) and the
WebSocket service initializer that runs at every boot. But ``src.models`` never
imported the module, so they were missing from ``Base.metadata`` where alembic
looks: the ``r6h3_model_baseline`` ``create_all`` skipped them, the blocking
``alembic check`` drift gate couldn't see them, and managed DBs (which skip
boot-time ``create_all``) never got them.

Covered here:

* the package registers all four tables, so the baseline and the drift
  gate cover them from now on;
* ``ws01_websocket_status_tables`` creates exactly the missing ones and
  leaves existing tables (and their rows) alone, and a second run is a
  no-op. This runs the real ``upgrade()`` through alembic's ``Operations`` on
  SQLite, in a child process (see ``_run``);
* the offline ``--sql`` render of the revision emits the four CREATE TABLEs;
* every table is locked down like ``rl01``: RLS enabled and ALL revoked
  from ``anon``/``authenticated``, whether it was just created or already
  existed; a pre-existing enum type with other labels, or the legacy Supabase
  ``websocket_connections`` shape, fails the upgrade loudly. The offline
  render carries the same guards and lockdown;
* ``alembic upgrade head`` from an EMPTY PostgreSQL ends with all four
  tables, RLS on and no PostgREST grants. These PostgreSQL tests need a
  scratch server whose admin URL is in ``GOO686_PG_URL``; each test creates
  and drops its own database. Skipped without one; in CI, the from-empty
  upgrade plus the blocking ``alembic check`` cover the create path.
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

pytestmark = pytest.mark.unit

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT = "rl01_secure_late_internal_rls"
REVISION = "ws01_websocket_status_tables"
TABLES = (
    "websocket_connections",
    "connection_events",
    "status_updates",
    "notification_templates",
)


def _run(code: str, *, env: dict[str, str] | None = None) -> str:
    """Run ``code`` in a child process from ``backend/``, return its stdout.

    A child process, because other migration tests stub ``sys.modules["alembic"]``
    during collection; the child always gets the real package.
    """
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_ROOT,
        env={**os.environ, "PYTHONPATH": str(BACKEND_ROOT), **(env or {})},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return proc.stdout


def test_model_package_registers_the_websocket_status_tables() -> None:
    out = _run(
        "import json, src.models\n"
        "from src.models.base import Base\n"
        f"print(json.dumps([t in Base.metadata.tables for t in {list(TABLES)!r}]))\n"
    )
    assert json.loads(out.strip().splitlines()[-1]) == [True] * len(TABLES)


_SQLITE_UPGRADE = r"""
import importlib.util, json, sys
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic import op as alembic_op

spec = importlib.util.spec_from_file_location(
    "ws01", "alembic/versions/ws01_websocket_status_tables.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

engine = sa.create_engine("sqlite://")
with engine.begin() as conn:
    # Pre-existing world: one of the four already exists, with a row.
    conn.exec_driver_sql(
        "CREATE TABLE notification_templates (id VARCHAR PRIMARY KEY, keep TEXT)"
    )
    conn.exec_driver_sql("INSERT INTO notification_templates VALUES ('t1', 'kept')")
    conn.exec_driver_sql("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")

runs = []
for _ in range(2):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()
        insp = sa.inspect(conn)
        runs.append(sorted(insp.get_table_names()))
        cols = [c["name"] for c in insp.get_columns("notification_templates")]
        row = conn.exec_driver_sql("SELECT keep FROM notification_templates").scalar()
        sc = [c["name"] for c in insp.get_columns("status_updates")]
print(json.dumps({"runs": runs, "nt_cols": cols, "row": row, "su_cols": sc}))
"""


def test_upgrade_creates_only_missing_tables_and_is_idempotent() -> None:
    result = json.loads(_run(_SQLITE_UPGRADE).strip().splitlines()[-1])

    first, second = result["runs"]
    for table in TABLES:
        assert table in first, table
    assert "unrelated" in first
    assert second == first  # a rerun changes nothing
    # The pre-existing table is not recreated or altered, and keeps its row.
    assert result["nt_cols"] == ["id", "keep"]
    assert result["row"] == "kept"
    # The created tables come from the ORM models, not a hand copy.
    for column in ("update_id", "update_type", "priority", "connection_id"):
        assert column in result["su_cols"], column


def _render(start: str = PARENT) -> str:
    return _run(
        "import sys\nfrom alembic.config import main\n"
        f"main(argv=['upgrade', '{start}:{REVISION}', '--sql'])\n",
        env={"DATABASE_URL": "postgresql://render:render@localhost:5432/render"},
    )


def test_offline_sql_render_creates_the_four_tables() -> None:
    sql = _render()
    for table in TABLES:
        assert f"CREATE TABLE {table} (" in sql, table
    assert "DROP TABLE" not in sql


def test_offline_render_secures_every_table_like_rl01() -> None:
    """The render carries the same guards and RLS lockdown as the online run."""
    sql = _render()
    for table in TABLES:
        assert f"""ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY""" in sql
        for role in ("anon", "authenticated"):
            assert f"""REVOKE ALL ON TABLE public."{table}" FROM {role}""" in sql
    # RLS comes after the CREATE TABLEs, the guards before them.
    first_create = sql.index("CREATE TABLE websocket_connections (")
    assert sql.index("ENABLE ROW LEVEL SECURITY") > first_create
    for guard in ("legacy Supabase shape", "existing enum type public.priority"):
        assert sql.index(guard) < first_create, guard
    # Expected enum labels come from the models, not a hand copy.
    assert "ARRAY['LOW', 'NORMAL', 'HIGH', 'CRITICAL']" in sql
    assert "'CONNECTING', 'CONNECTED'" in sql
    # PL/pgSQL RAISE placeholders are a single % (a doubled one is a literal).
    assert "has labels %, expected %;" in sql and "%%" not in sql


_PG_URL = os.environ.get("GOO686_PG_URL")
_needs_pg = pytest.mark.skipif(
    not _PG_URL,
    reason="needs a scratch PostgreSQL server (admin URL) in GOO686_PG_URL",
)


@pytest.fixture
def scratch_db() -> Iterator[str]:
    """A brand-new empty database on the GOO686_PG_URL server; dropped after."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    assert _PG_URL is not None
    admin = make_url(_PG_URL)
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


def _pg(url: str, sql: str) -> list:
    """Run ``;;``-separated ``sql`` on ``url``; return the last statement's rows."""
    out = _run(
        "import json, os, sqlalchemy as sa\n"
        "e = sa.create_engine(os.environ['DATABASE_URL'])\n"
        "rows = []\n"
        "with e.begin() as c:\n"
        f"    for stmt in {sql!r}.split(';;'):\n"
        "        r = c.exec_driver_sql(stmt)\n"
        "        rows = [list(x) for x in r] if r.returns_rows else []\n"
        "print(json.dumps(rows, default=str))\n",
        env={"DATABASE_URL": url},
    )
    rows: list = json.loads(out.strip().splitlines()[-1])
    return rows


def _upgrade(url: str, target: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=BACKEND_ROOT,
        env={**os.environ, "PYTHONPATH": str(BACKEND_ROOT), "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=900,
    )


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


@_needs_pg
def test_alembic_upgrade_head_from_empty_postgres_has_locked_down_tables(
    scratch_db: str,
) -> None:
    # Supabase-like: the PostgREST roles exist and get default grants.
    _pg(scratch_db, _POSTGREST_ROLES_SQL)
    proc = _upgrade(scratch_db, "head")
    assert proc.returncode == 0, proc.stderr[-4000:]
    _assert_locked_down(scratch_db)


@_needs_pg
def test_existing_exposed_tables_are_locked_down_and_missing_ones_created(
    scratch_db: str,
) -> None:
    """The backfill path: a DB past rl01 where some tables are missing and an
    existing one is world-readable (RLS off, PostgREST grants)."""
    _pg(scratch_db, _POSTGREST_ROLES_SQL)
    proc = _upgrade(scratch_db, PARENT)
    assert proc.returncode == 0, proc.stderr[-4000:]
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


@_needs_pg
def test_upgrade_refuses_the_legacy_supabase_websocket_connections_shape(
    scratch_db: str,
) -> None:
    proc = _upgrade(scratch_db, PARENT)
    assert proc.returncode == 0, proc.stderr[-4000:]
    _pg(
        scratch_db,
        "DROP TABLE public.connection_events, public.status_updates, "
        "public.websocket_connections CASCADE;;"
        "CREATE TABLE public.websocket_connections (id uuid PRIMARY KEY, "
        "connection_id varchar(255) NOT NULL, user_id uuid NOT NULL, client_ip inet)",
    )
    proc = _upgrade(scratch_db, REVISION)
    assert proc.returncode != 0
    assert "legacy Supabase shape" in proc.stderr
    # Nothing was created: the guard runs before any DDL.
    [[status_updates]] = _pg(scratch_db, "SELECT to_regclass('public.status_updates')")
    assert status_updates is None


@_needs_pg
def test_upgrade_refuses_an_existing_enum_with_other_labels(scratch_db: str) -> None:
    proc = _upgrade(scratch_db, PARENT)
    assert proc.returncode == 0, proc.stderr[-4000:]
    _pg(
        scratch_db,
        "DROP TABLE public.status_updates, public.notification_templates CASCADE;;"
        "DROP TYPE public.priority;;"
        "CREATE TYPE public.priority AS ENUM ('low', 'high')",
    )
    proc = _upgrade(scratch_db, REVISION)
    assert proc.returncode != 0
    assert "existing enum type public.priority" in proc.stderr
