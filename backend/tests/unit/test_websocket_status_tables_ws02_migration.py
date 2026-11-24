"""GOO-690: ``ws02_realtime_status_hardening`` without a database server.

ws01 (GOO-686) is applied on dev and stays as on develop; ws02 carries the
#2229 follow-ups. Covered here:

* on SQLite, ws02 creates exactly the missing tables from the ORM models,
  leaves existing ones (and their rows) alone, and a rerun is a no-op;
* the offline ``--sql`` render of ``ws01:ws02``:
  - the guards come before any DDL: every table's column guard (naming the
    missing columns, with the diagnostic query), the enum-label guard and the
    non-enum guard;
  - ensure-create is idempotent (``to_regtype`` / ``to_regclass``), creates
    only the four tables' own three enum types, and carries every model index;
  - every table is locked down like ``rl01`` after the creates.

The PostgreSQL behaviour is in
``test_websocket_status_tables_migration_postgres.py``.
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
PARENT = "ws01_websocket_status_tables"
REVISION = "ws02_realtime_status_hardening"
TABLES = (
    "websocket_connections",
    "connection_events",
    "status_updates",
    "notification_templates",
)
OWN_ENUM_TYPES = {"connectionstatus", "updatetype", "priority"}


def _run(code: str, *, env: dict[str, str] | None = None) -> str:
    """Run ``code`` in a child process from ``backend/``; return its stdout.

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


def test_ws02_revises_ws01() -> None:
    out = _run(
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('ws02', "
        f"'alembic/versions/{REVISION}.py')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "print(mod.revision, mod.down_revision)\n"
    )
    assert out.split() == [REVISION, PARENT]


_SQLITE_UPGRADE = r"""
import importlib.util, json
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

spec = importlib.util.spec_from_file_location(
    "ws02", "alembic/versions/ws02_realtime_status_hardening.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

engine = sa.create_engine("sqlite://")
with engine.begin() as conn:
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


def test_sqlite_upgrade_creates_only_missing_tables_and_is_idempotent() -> None:
    result = json.loads(_run(_SQLITE_UPGRADE).strip().splitlines()[-1])

    first, second = result["runs"]
    for table in TABLES:
        assert table in first, table
    assert "unrelated" in first
    assert second == first
    assert result["nt_cols"] == ["id", "keep"]
    assert result["row"] == "kept"
    for column in ("update_id", "update_type", "priority", "connection_id"):
        assert column in result["su_cols"], column


def _render() -> str:
    return _run(
        "from alembic.config import main\n"
        f"main(argv=['upgrade', '{PARENT}:{REVISION}', '--sql'])\n",
        env={"DATABASE_URL": "postgresql://render:render@localhost:5432/render"},
    )


def test_offline_render_ensures_only_its_own_tables_types_and_indexes() -> None:
    sql = _render()
    for table in TABLES:
        assert sql.count(f"CREATE TABLE {table} (") == 1, table
        assert f"""IF to_regclass('public."{table}"') IS NULL THEN""" in sql
    assert "DROP " not in sql
    created_types = re.findall(r"CREATE TYPE public\.(\w+) AS ENUM", sql)
    assert sorted(created_types) == sorted(OWN_ENUM_TYPES)
    for own in OWN_ENUM_TYPES:
        assert f"IF to_regtype('public.{own}') IS NULL THEN" in sql, own
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
    # FK order: the referenced table is created first.
    assert sql.index("CREATE TABLE websocket_connections (") < sql.index(
        "CREATE TABLE status_updates ("
    )


def test_offline_render_guards_first_and_locks_down_every_table() -> None:
    sql = _render()
    first_ddl = sql.index("CREATE TYPE public.")
    for guard in (
        "is missing column(s)",
        "existing enum type public.priority",
        "is not an enum type",
    ):
        assert sql.index(guard) < first_ddl, guard
    for table in TABLES:
        assert f"existing public.{table} is missing column(s) %" in sql, table
        assert f"table_name = ''{table}'' ORDER BY ordinal_position" in sql, table
        assert f"""ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY""" in sql
        for role in ("anon", "authenticated"):
            assert f"""REVOKE ALL ON TABLE public."{table}" FROM {role}""" in sql
    last_create = sql.rindex("CREATE TABLE ")
    assert sql.index("ENABLE ROW LEVEL SECURITY") > last_create
    assert "ARRAY['LOW', 'NORMAL', 'HIGH', 'CRITICAL']" in sql
    assert "'CONNECTING', 'CONNECTED'" in sql
    # PL/pgSQL RAISE placeholders are a single % (a doubled one is a literal).
    assert "has labels %, expected %;" in sql and "%%" not in sql
