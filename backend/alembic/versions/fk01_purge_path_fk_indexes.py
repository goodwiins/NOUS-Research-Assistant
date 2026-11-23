"""Index the foreign keys a thread purge has to check (GOO-655).

Revision ID: fk01_purge_path_fk_indexes
Revises: ws01_websocket_status_tables
Create Date: 2026-11-23 00:00:00

``purge_soft_deleted_threads`` hard-deletes a chat's ``chat_messages`` and
then its ``threads`` row with bulk DELETEs, and the CASCADE rules carry the
delete on into the tables listed in
``tests/unit/models/test_thread_purge_fk_contract.py``. For every deleted
row PostgreSQL checks each foreign key that points at it with
``WHERE <fk column> = $1`` on the referencing table. Without an index whose
leading column is that FK, each check is a sequential scan, so a purge
cost grows with (deleted rows) x (rows in every referencing table).

These are the 13 foreign keys into purged tables that had no such index,
found by querying ``pg_constraint``/``pg_index`` on a from-empty database at
``ws01_websocket_status_tables``. The other unindexed FKs in the schema are
deliberately left alone: an index costs every insert and update, and those
FKs are not on a hot delete path.

Locking: ``CREATE INDEX CONCURRENTLY`` takes SHARE UPDATE EXCLUSIVE, so
reads and writes continue during the build. It cannot run in a transaction,
so each statement runs and commits on its own in ``autocommit_block``.

An interrupted concurrent build leaves its index behind marked INVALID, and
``IF NOT EXISTS`` would then accept it while the index does nothing. Each
index is therefore dropped (concurrently) first if it exists and is
invalid, so re-running the upgrade repairs a half-built index. Lookups use
``to_regclass`` so they follow ``search_path``.
"""

import alembic.op as op
from sqlalchemy import text

revision = "fk01_purge_path_fk_indexes"
down_revision = "ws01_websocket_status_tables"
branch_labels = None
depends_on = None

# (table, column). Names follow SQLAlchemy's default ``ix_<table>_<column>``
# so the ``index=True`` declarations on the models match for ``alembic check``.
FK_COLUMNS = (
    ("agent_runs", "user_message_id"),
    ("agent_runs", "assistant_message_id"),
    ("artifact_lifecycle_outbox", "thread_id"),
    ("artifact_references", "message_id"),
    ("artifact_versions", "thread_id"),
    ("chat_messages", "superseded_by_message_id"),
    ("draft_citations", "citation_id"),
    ("integration_grants", "thread_id"),
    ("integration_handoffs", "consent_id"),
    ("integration_handoffs", "thread_id"),
    ("integration_tool_actions", "thread_id"),
    ("native_artifact_intents", "thread_id"),
    ("threads", "selected_message_id"),
)


def _name(table: str, column: str) -> str:
    return f"ix_{table}_{column}"


def _is_invalid(name: str) -> bool:
    # Static identifiers only.
    return bool(
        op.get_bind()
        .execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_index "
                "WHERE indexrelid = to_regclass(:name) AND NOT indisvalid)"
            ),
            {"name": name},
        )
        .scalar()
    )


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for table, column in FK_COLUMNS:
            name = _name(table, column)
            if _is_invalid(name):
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                f"ON {table} ({column})"
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for table, column in reversed(FK_COLUMNS):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_name(table, column)}")
