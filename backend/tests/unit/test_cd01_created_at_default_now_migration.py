"""GOO-694: offline ``alembic upgrade head --sql`` must not crash in cd01.

``cd01_created_at_default_now`` used to call ``sa.inspect(op.get_bind())``
unconditionally, which raises ``NoInspectionAvailable`` against Alembic's
``MockConnection`` in offline (``--sql``) mode.

Runs Alembic in a child process so this test never imports ``alembic`` in the
pytest process (see ``tests/unit/ci/test_migration_tests_collect_together.py``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND_ROOT = Path(__file__).resolve().parents[2]
FROZEN_DEFAULT_COLUMNS = (
    ("encrypted_user_profiles", "created_at"),
    ("encrypted_organization_profiles", "created_at"),
    ("encryption_audit_logs", "created_at"),
    ("entity_relationships", "created_at"),
    ("entity_categories", "assigned_at"),
    ("entity_synonyms", "created_at"),
)


def _run(code: str, *, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_ROOT,
        env={**os.environ, "PYTHONPATH": str(BACKEND_ROOT), **(env or {})},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return proc.stdout


def test_offline_upgrade_head_sql_from_empty_succeeds() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=BACKEND_ROOT,
        env={
            **os.environ,
            "DATABASE_URL": "postgresql://user:pass@localhost:5432/ragdb",
            "SUPABASE_DB_URL": "",
        },
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    sql = proc.stdout
    marker = "-- Running upgrade mh01_machine_handoff -> cd01_created_at_default_now"
    end_marker = "UPDATE alembic_version SET version_num='cd01_created_at_default_now'"
    assert marker in sql and end_marker in sql
    cd01_section = sql[sql.index(marker) : sql.index(end_marker)]
    for table, column in FROZEN_DEFAULT_COLUMNS:
        expected = f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET DEFAULT now();'
        assert expected in cd01_section, (table, column)
    assert "END $$;;" not in cd01_section


_BARE_OFFLINE_CONTEXT = r"""
import importlib.util, io
from alembic.migration import MigrationContext
from alembic.operations import Operations

spec = importlib.util.spec_from_file_location(
    "cd01", "alembic/versions/cd01_created_at_default_now.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

buf = io.StringIO()
ctx = MigrationContext.configure(
    url="postgresql://user:pass@localhost:5432/ragdb",
    opts={"as_sql": True, "output_buffer": buf},
)
with Operations.context(ctx):
    mod.upgrade()
print(buf.getvalue())
"""


def test_bare_operations_offline_mode_emits_guarded_ddl() -> None:
    out = _run(_BARE_OFFLINE_CONTEXT)
    for table, column in FROZEN_DEFAULT_COLUMNS:
        assert f"table_name = '{table}'" in out
        assert f"column_name = '{column}'" in out
        assert (
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET DEFAULT now();' in out
        )


_ONLINE_INSPECTION = r"""
import importlib.util, json
import sqlalchemy as sa
from alembic import op as alembic_op
from alembic.migration import MigrationContext
from alembic.operations import Operations

spec = importlib.util.spec_from_file_location(
    "cd01", "alembic/versions/cd01_created_at_default_now.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

engine = sa.create_engine("sqlite://")
with engine.begin() as conn:
    conn.exec_driver_sql(
        "CREATE TABLE encryption_audit_logs (id INTEGER PRIMARY KEY, created_at TEXT)"
    )
    conn.exec_driver_sql(
        "CREATE TABLE entity_categories (id INTEGER PRIMARY KEY, other_col TEXT)"
    )

altered = []
orig_alter = alembic_op.alter_column
alembic_op.alter_column = lambda table, col, **kw: altered.append((table, col, str(kw.get("server_default"))))
try:
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()
finally:
    alembic_op.alter_column = orig_alter

print(json.dumps(altered))
"""


def test_online_mode_alters_only_existing_table_and_column() -> None:
    import json

    out = _run(_ONLINE_INSPECTION)
    altered = json.loads(out.strip().splitlines()[-1])
    assert altered == [["encryption_audit_logs", "created_at", "now()"]]
