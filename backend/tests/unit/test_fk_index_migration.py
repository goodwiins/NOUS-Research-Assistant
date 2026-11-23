"""Offline checks of the fk01_purge_path_fk_indexes migration (GOO-655).

``alembic upgrade FROM:TO --sql`` renders the revision without a database.
It used to crash (``op.get_bind()`` is None offline) because the INVALID-index
check ran unconditionally. Same subprocess pattern as
``test_thread_fk_ondelete_migration.py``: never import alembic in this
process (``tests/unit/ci/test_migration_tests_collect_together.py``).

Executing the revision against PostgreSQL is
``tests/integration/test_fk_index_migration_postgres.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND_ROOT = Path(__file__).resolve().parents[1].parent
PARENT = "ws01_websocket_status_tables"
REVISION = "fk01_purge_path_fk_indexes"
# Mirrors the revision.
FK_COLUMNS = (
    ("agent_runs", "thread_id"),
    ("agent_runs", "user_message_id"),
    ("agent_runs", "assistant_message_id"),
    ("artifact_lifecycle_outbox", "thread_id"),
    ("artifact_references", "message_id"),
    ("artifact_versions", "thread_id"),
    ("chat_messages", "parent_message_id"),
    ("chat_messages", "superseded_by_message_id"),
    ("draft_citations", "citation_id"),
    ("integration_grants", "thread_id"),
    ("integration_handoffs", "consent_id"),
    ("integration_handoffs", "thread_id"),
    ("integration_tool_actions", "thread_id"),
    ("native_artifact_intents", "thread_id"),
    ("threads", "selected_message_id"),
)


def _alembic(*alembic_args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k != "SUPABASE_DB_URL"}
    # Only the dialect is read from the URL; nothing connects.
    env["DATABASE_URL"] = "postgresql://offline.invalid/nous"
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *alembic_args],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    return done.stdout


def _render(*alembic_args: str) -> str:
    return re.sub(r"\s+", " ", _alembic(*alembic_args, "--sql"))


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return _render("upgrade", f"{PARENT}:{REVISION}")


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    return _render("downgrade", f"{REVISION}:{PARENT}")


def test_upgrade_renders_offline_and_builds_every_index(upgrade_sql: str) -> None:
    for table, column in FK_COLUMNS:
        statement = (
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_{table}_{column} "
            f"ON {table} ({column});"
        )
        assert statement in upgrade_sql
    assert upgrade_sql.count("CREATE INDEX CONCURRENTLY") == len(FK_COLUMNS)


def test_upgrade_is_bounded_by_lock_timeout(upgrade_sql: str) -> None:
    first_create = upgrade_sql.index("CREATE INDEX CONCURRENTLY")
    assert upgrade_sql.index("SET lock_timeout = '30s';") < first_create
    assert upgrade_sql.rindex("RESET lock_timeout;") > upgrade_sql.rindex(
        "CREATE INDEX CONCURRENTLY"
    )
    # No statement_timeout: a healthy large build must not be capped.
    assert "statement_timeout" not in upgrade_sql


def test_offline_render_skips_the_invalid_index_probe(upgrade_sql: str) -> None:
    assert "pg_index" not in upgrade_sql
    assert "DROP INDEX" not in upgrade_sql


def test_downgrade_drops_every_index_concurrently(downgrade_sql: str) -> None:
    for table, column in FK_COLUMNS:
        assert (
            f"DROP INDEX CONCURRENTLY IF EXISTS ix_{table}_{column};" in downgrade_sql
        )
