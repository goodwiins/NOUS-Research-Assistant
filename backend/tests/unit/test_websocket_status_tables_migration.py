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
* the PostgreSQL behaviour (from-empty ``upgrade head``, the backfill path,
  only-own enum types, and every fail-loud guard on a real database) lives
  in ``test_websocket_status_tables_migration_postgres.py``, which the CI
  PostgreSQL lane runs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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


OWN_ENUM_TYPES = {"connectionstatus", "updatetype", "priority"}


def test_offline_sql_render_creates_the_four_tables() -> None:
    sql = _render()
    for table in TABLES:
        assert sql.count(f"CREATE TABLE {table} (") == 1, table
    assert "DROP TABLE" not in sql
    # Only the four tables' own enum types, each exactly once; nothing foreign.
    created_types = re.findall(r"CREATE TYPE (\w+) AS ENUM", sql)
    assert sorted(created_types) == sorted(OWN_ENUM_TYPES)
    # Their indexes render too (the models declare index=True columns).
    out = _run(
        "import json, src.models\n"
        "from src.models.base import Base\n"
        f"names = [i.name for t in {list(TABLES)!r} for i in Base.metadata.tables[t].indexes]\n"
        "print(json.dumps(names))\n"
    )
    index_names = json.loads(out.strip().splitlines()[-1])
    assert index_names
    for name in index_names:
        assert (
            f"CREATE INDEX {name} " in sql or f"CREATE UNIQUE INDEX {name} " in sql
        ), name


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
    for guard in (
        "is missing column(s)",
        "existing enum type public.priority",
        "is not an enum type",
    ):
        assert sql.index(guard) < first_create, guard
    # Expected enum labels come from the models, not a hand copy.
    assert "ARRAY['LOW', 'NORMAL', 'HIGH', 'CRITICAL']" in sql
    assert "'CONNECTING', 'CONNECTED'" in sql
    # PL/pgSQL RAISE placeholders are a single % (a doubled one is a literal).
    assert "has labels %, expected %;" in sql and "%%" not in sql
    # Every table's column guard names the expected columns and carries the
    # diagnostic query.
    for table in TABLES:
        assert f"existing public.{table} is missing column(s) %" in sql, table
        assert f"table_name = ''{table}'' ORDER BY ordinal_position" in sql, table
