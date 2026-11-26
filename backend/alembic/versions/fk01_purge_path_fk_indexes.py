"""Index the foreign keys a thread purge has to check (GOO-655).

Revision ID: fk01_purge_path_fk_indexes
Revises: ws02_realtime_status_hardening
Create Date: 2026-11-23 00:00:00

``purge_soft_deleted_threads`` hard-deletes a chat's ``chat_messages`` and
then its ``threads`` row with bulk DELETEs, and the CASCADE rules carry the
delete on into the tables listed in
``tests/unit/architecture/test_thread_purge_fk_contract.py``. For every deleted
row PostgreSQL checks each foreign key that points at it with
``WHERE <fk column> = $1`` on the referencing table. Without an index whose
leading column is that FK, each check is a sequential scan, so a purge
cost grows with (deleted rows) x (rows in every referencing table).

These are the 15 foreign keys into purged tables that had no such index,
found by querying ``pg_constraint``/``pg_index`` on a from-empty database at
``ws01_websocket_status_tables``. A partial index, or one where the FK is
not the leading column, does not serve the check: ``agent_runs.thread_id``
has only the partial ``uq_agent_runs_active_thread``, and
``chat_messages.parent_message_id`` only leads a partial index and is second
in ``ix_chat_messages_parent``. The other unindexed FKs in the schema are
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

Lock waits: a concurrent build waits for every transaction that holds a
conflicting lock on the table, including one left idle in transaction.
Each statement runs under ``lock_timeout`` (``_LOCK_TIMEOUT``), so a blocked
build fails fast with a message naming the index, instead of hanging until
the migration Job's deadline kills the pod. A build cancelled that way
leaves an INVALID index, which the next upgrade drops and rebuilds. There is
deliberately no ``statement_timeout``: the build itself scales with table
size, and capping it would fail a healthy large build.

Offline (``--sql``): there is no database to ask about INVALID indexes, so
the check is skipped and only the SET/CREATE statements are rendered.
"""

import logging

import alembic.op as op
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

revision = "fk01_purge_path_fk_indexes"
down_revision = "ws02_realtime_status_hardening"
branch_labels = None
depends_on = None

# (table, column). Names follow SQLAlchemy's default ``ix_<table>_<column>``
# so the ``index=True`` declarations on the models match for ``alembic check``.
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

logger = logging.getLogger(__name__)

# Per statement. Read at call time, so the PostgreSQL test can shorten it.
_LOCK_TIMEOUT = "30s"
_LOCK_NOT_AVAILABLE = "55P03"


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


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def _build(table: str, column: str, offline: bool) -> None:
    name = _name(table, column)
    # The repair DROP waits on the same locks as the build, so both sit under
    # the same lock_timeout and the same friendly error.
    try:
        if not offline and _is_invalid(name):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({column})"
        )
    except DBAPIError as exc:
        if _sqlstate(exc) != _LOCK_NOT_AVAILABLE:
            raise
        raise RuntimeError(
            f"index {name} on {table}: gave up after lock_timeout "
            f"{_LOCK_TIMEOUT} waiting for other transactions on {table}. "
            f"{name} may be left INVALID. Find the blocking sessions "
            "(pg_stat_activity, often 'idle in transaction'), end them, and "
            "re-run `alembic upgrade head`: it rebuilds INVALID indexes."
        ) from exc


def _reset_after_failure() -> None:
    """Best-effort RESET; after a terminated backend the connection is dead
    and this fails too, which must not replace the error being raised."""
    try:
        op.execute("RESET lock_timeout")
    except Exception:
        logger.warning(
            "RESET lock_timeout failed after a failed upgrade", exc_info=True
        )


def upgrade() -> None:
    offline = bool(op.get_context().as_sql)
    failure: BaseException | None = None
    try:
        with op.get_context().autocommit_block():
            op.execute(f"SET lock_timeout = '{_LOCK_TIMEOUT}'")
            try:
                for table, column in FK_COLUMNS:
                    _build(table, column, offline)
            except BaseException as exc:
                failure = exc
                _reset_after_failure()
                raise
            op.execute("RESET lock_timeout")
    except BaseException as exc:
        # On a dead connection alembic's autocommit_block exit raises its own
        # PendingRollbackError over ours; report the build's error instead.
        if failure is not None and exc is not failure:
            logger.warning(
                "autocommit_block exit failed after a failed upgrade", exc_info=True
            )
            raise failure from None
        raise


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for table, column in reversed(FK_COLUMNS):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_name(table, column)}")
