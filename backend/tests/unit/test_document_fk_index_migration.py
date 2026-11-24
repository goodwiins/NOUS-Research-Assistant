"""Offline checks of the fk02_document_delete_fk_indexes migration (GOO-737).

``alembic upgrade FROM:TO --sql`` renders the revision without a database.
Same subprocess pattern as ``test_fk_index_migration.py`` (fk01): never
import alembic in this process
(``tests/unit/ci/test_migration_tests_collect_together.py``).

The models must declare the same indexes; that is
``tests/unit/architecture/test_thread_purge_fk_contract.py``.
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
PARENT = "fk01_purge_path_fk_indexes"
REVISION = "fk02_document_delete_fk_indexes"
# Mirrors the revision.
FK_COLUMNS = (
    ("agent_runs", "conversation_id"),
    ("agent_runs", "project_id"),
    ("entities", "document_id"),
    ("extraction_accepted_values", "document_id"),
    ("research_claim_evidence_links", "document_id"),
    ("research_fulltext_attempts", "document_id"),
    ("search_results", "document_id"),
    ("threads", "branch_document_id"),
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


def test_every_index_is_checked_valid_after_its_build(upgrade_sql: str) -> None:
    # The revision is never stamped over a missing or INVALID index, and the
    # check survives into an offline script (same SQL as PR #2366's helper).
    for table, column in FK_COLUMNS:
        name = f"ix_{table}_{column}"
        check = f"RAISE EXCEPTION 'Expected valid index {name} on {table}';"
        assert check in upgrade_sql
        assert upgrade_sql.index(check) > upgrade_sql.index(f"{name} ON {table}")
    assert upgrade_sql.count("AND i.indisvalid") == len(FK_COLUMNS)


def test_upgrade_is_bounded_by_lock_timeout(upgrade_sql: str) -> None:
    first_create = upgrade_sql.index("CREATE INDEX CONCURRENTLY")
    assert upgrade_sql.index("SET lock_timeout = '30s';") < first_create
    assert upgrade_sql.rindex("RESET lock_timeout;") > upgrade_sql.rindex(
        "CREATE INDEX CONCURRENTLY"
    )
    # No statement_timeout: a healthy large build must not be capped.
    assert "statement_timeout" not in upgrade_sql


def test_offline_render_skips_the_invalid_index_repair(upgrade_sql: str) -> None:
    assert "NOT indisvalid" not in upgrade_sql
    assert "DROP INDEX" not in upgrade_sql


def test_downgrade_drops_every_index_concurrently(downgrade_sql: str) -> None:
    for table, column in FK_COLUMNS:
        assert (
            f"DROP INDEX CONCURRENTLY IF EXISTS ix_{table}_{column};" in downgrade_sql
        )
    assert downgrade_sql.count("DROP INDEX CONCURRENTLY") == len(FK_COLUMNS)
