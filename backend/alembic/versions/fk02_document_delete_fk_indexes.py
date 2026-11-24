"""Index the foreign keys a document hard-delete has to check (GOO-737).

Revision ID: fk02_document_delete_fk_indexes
Revises: fk01_purge_path_fk_indexes
Create Date: 2026-11-24 00:00:00

The remainder of fk01_purge_path_fk_indexes (GOO-655), which indexed the
foreign keys a thread purge checks. Deleting a ``documents`` row makes
PostgreSQL check every foreign key that points at it, and at every row the
CASCADE rules delete in turn, with ``WHERE <fk column> = $1`` on the
referencing table. That includes RESTRICT and NO ACTION keys, whose check is
what refuses the delete. Without an index whose leading column is that FK,
each check is a sequential scan, so a delete costs (deleted rows) x (rows in
every referencing table). ``tests/unit/architecture/test_thread_purge_fk_contract.py``
walks the same path from ``documents`` and requires an index for each FK.

These six FKs on that path had no such index in the models or in any
revision: ``entities.document_id`` and ``search_results.document_id``
(CASCADE), ``threads.branch_document_id`` (SET NULL), and
``research_claim_evidence_links.document_id``,
``research_fulltext_attempts.document_id`` and
``extraction_accepted_values.document_id`` (RESTRICT). The last one leads
only the partial ``uq_extraction_accepted_initial``, which does not serve the
check for every row. The two ``agent_runs`` FKs, ``conversation_id`` and
``project_id`` (SET NULL), are on the conversation and collection delete
paths and were reported with them.

Locking: ``CREATE INDEX CONCURRENTLY`` takes SHARE UPDATE EXCLUSIVE, so
reads and writes continue during the build. It cannot run in a transaction,
so each statement runs and commits on its own in ``autocommit_block``.

An interrupted concurrent build leaves its index behind marked INVALID, and
``IF NOT EXISTS`` would then accept it while the index does nothing. Each
index is therefore dropped (concurrently) first if it exists and is
invalid, so re-running the upgrade repairs a half-built index. After each
build the revision checks that the index exists and is valid, and fails
otherwise, so it is never stamped over an INVALID index. The check is SQL,
the same as ``require_valid_index`` in ``src/core/migration_indexes.py``
(PR #2366), so it also runs from an offline script. Lookups use
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
the repair probe is skipped and only the SET, CREATE, validity check and
RESET statements are rendered.
"""

import logging

import alembic.op as op
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

revision = "fk02_document_delete_fk_indexes"
down_revision = "fk01_purge_path_fk_indexes"
branch_labels = None
depends_on = None

# (table, column). Names follow SQLAlchemy's default ``ix_<table>_<column>``
# so the index declarations on the models match for ``alembic check``.
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


def _require_valid(table: str, name: str) -> None:
    """Fail unless ``name`` on ``table`` exists and is valid.

    Same SQL as ``require_valid_index`` from PR #2366, without its trailing
    semicolon (an offline render adds one); a copy, so this revision does not
    depend on it. Static identifiers only.
    """
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_index i
                JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
                WHERE i.indrelid = pg_catalog.to_regclass('{table}')
                  AND c.relname = '{name}'
                  AND i.indisvalid
            ) THEN
                RAISE EXCEPTION 'Expected valid index {name} on {table}';
            END IF;
        END
        $$
        """)


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
    _require_valid(table, name)


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
