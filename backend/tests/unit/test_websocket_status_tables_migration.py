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
* ``alembic upgrade head`` from an EMPTY PostgreSQL ends with all four
  tables. It needs a scratch database in ``GOO686_PG_URL`` and is skipped
  without one (NOT RUN locally; CI's from-empty upgrade plus the blocking
  ``alembic check`` cover it there).
"""

from __future__ import annotations

import json
import os
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


def test_offline_sql_render_creates_the_four_tables() -> None:
    sql = _run(
        "import sys\nfrom alembic.config import main\n"
        f"main(argv=['upgrade', '{PARENT}:{REVISION}', '--sql'])\n",
        env={"DATABASE_URL": "postgresql://render:render@localhost:5432/render"},
    )
    for table in TABLES:
        assert f"CREATE TABLE {table} (" in sql, table
    assert "DROP TABLE" not in sql


_PG_URL = os.environ.get("GOO686_PG_URL")


@pytest.mark.skipif(
    not _PG_URL, reason="needs an empty scratch PostgreSQL in GOO686_PG_URL"
)
def test_alembic_upgrade_head_from_empty_postgres_has_the_tables() -> None:
    assert _PG_URL is not None
    _run(
        "from alembic.config import main\nmain(argv=['upgrade', 'head'])\n",
        env={"DATABASE_URL": _PG_URL},
    )
    out = _run(
        "import json, os, sqlalchemy as sa\n"
        "insp = sa.inspect(sa.create_engine(os.environ['DATABASE_URL']))\n"
        "print(json.dumps(sorted(insp.get_table_names())))\n",
        env={"DATABASE_URL": _PG_URL},
    )
    tables = json.loads(out.strip().splitlines()[-1])
    for table in TABLES:
        assert table in tables, table
