"""Every foreign key a thread hard-delete reaches says what happens to its row.

``purge_soft_deleted_threads`` (src/tasks/retention_tasks.py) deletes a
chat's ``chat_messages`` and then the thread with bulk DELETEs: no ORM cascade
runs, so the database's ON DELETE rule is all that clears a referencing row. A
foreign key with no rule (NO ACTION) into any table that delete reaches makes
PostgreSQL refuse it. Seven thread FKs added 09-28..10-05 plus
``artifact_references.message_id`` did exactly that and stopped the purge for
every organization (audit HO-2, 2026-10-08).

Start from the two tables the purge deletes, follow every CASCADE to the
tables it deletes from in turn, and require CASCADE or SET NULL on every FK
into any of them. A new table that names a chat fails here until it picks
one. This reads the model metadata only; the rp01 migration and
tests/integration/test_retention_thread_purge_postgres.py hold the live
database to these rules for rp01's 11 FKs. A new FK's migration is not
checked (see docs/engineering/gotchas.md).

The index rule (every FK the delete reaches leads a full index) also holds
for a document hard-delete, walked the same way from ``documents``
(GOO-737). Its ON DELETE rules are not checked: RESTRICT there is deliberate.
The fk01 and fk02 revisions build the indexes on live databases.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, ForeignKey, Index, Integer, MetaData, Table

import src.models  # noqa: F401  register every table on Base.metadata
from src.models.base import Base

pytestmark = pytest.mark.unit

# What purge_soft_deleted_threads deletes itself.
PURGED = frozenset({"threads", "chat_messages"})
# A document hard-delete (GOO-737): the row and whatever its CASCADEs reach.
DOCUMENT_DELETE = frozenset({"documents"})
RULES = frozenset({"CASCADE", "SET NULL"})


def _walk(
    metadata: MetaData = Base.metadata, roots: frozenset[str] = PURGED
) -> tuple[set[str], list[str]]:
    """Tables a delete from ``roots`` reaches, and every FK into them with no
    rule PostgreSQL can carry out."""
    reached = set(roots)
    unruled: set[str] = set()
    grew = True
    while grew:
        grew = False
        for table in metadata.tables.values():
            for fk in table.foreign_keys:
                if fk.column.table.name not in reached:
                    continue
                rule = (fk.ondelete or "NO ACTION").upper()
                if rule not in RULES:
                    unruled.add(
                        f"{table.name}.{fk.parent.name} -> {fk.target_fullname}"
                    )
                elif rule == "SET NULL" and not fk.parent.nullable:
                    # PostgreSQL refuses the delete with a not-null violation,
                    # which wedges the purge just as NO ACTION does.
                    unruled.add(
                        f"{table.name}.{fk.parent.name} -> {fk.target_fullname}"
                        " (SET NULL on NOT NULL)"
                    )
                elif rule == "CASCADE" and table.name not in reached:
                    reached.add(table.name)
                    grew = True
    return reached, sorted(unruled)


def test_every_fk_a_thread_delete_reaches_has_an_on_delete_rule() -> None:
    _, unruled = _walk()
    assert unruled == []


def test_the_walk_follows_cascades_into_the_consent_tables() -> None:
    # Guards the guard: a walk that stopped at threads would never check the
    # FKs into the consent a chat-bound grant request cascades to.
    reached, _ = _walk()
    assert {
        "integration_handoffs",
        "integration_grant_requests",
        "integration_context_selections",
    } <= reached


def test_the_walk_rejects_set_null_on_a_not_null_column() -> None:
    # Guards the guard: a non-Optional Mapped[...] column is NOT NULL, so
    # ondelete="SET NULL" on it passes a rule-name check and still wedges the
    # purge. A nullable SET NULL column is fine and must not be reported.
    metadata = MetaData()
    Table("threads", metadata, Column("id", Integer, primary_key=True))
    Table("chat_messages", metadata, Column("id", Integer, primary_key=True))
    Table(
        "notes",
        metadata,
        Column("id", Integer, primary_key=True),
        Column(
            "thread_id",
            ForeignKey("threads.id", ondelete="SET NULL"),
            nullable=False,
        ),
        Column("message_id", ForeignKey("chat_messages.id", ondelete="SET NULL")),
    )
    _, unruled = _walk(metadata)
    assert unruled == ["notes.thread_id -> threads.id (SET NULL on NOT NULL)"]


def _leading_index_columns(table: Table) -> set[str]:
    """Columns that lead some full index, unique constraint or primary key.

    A partial index (``WHERE ...``) covers only some rows, so it cannot serve
    the RI check for every deleted parent; a column that is not first in an
    index is not served by it either.
    """
    leading: set[str] = set()
    for index in table.indexes:
        if any(
            index.dialect_options[dialect].get("where") is not None
            for dialect in ("postgresql", "sqlite")
        ):
            continue
        columns = list(index.columns)
        if columns:
            leading.add(columns[0].name)
    for constraint in table.constraints:
        columns = list(getattr(constraint, "columns", []))
        if columns and type(constraint).__name__ in (
            "UniqueConstraint",
            "PrimaryKeyConstraint",
        ):
            leading.add(columns[0].name)
    for column in table.columns:
        if column.index or column.unique or column.primary_key:
            leading.add(column.name)
    return leading


def _unindexed(
    metadata: MetaData = Base.metadata, roots: frozenset[str] = PURGED
) -> list[str]:
    """FKs into a table a delete from ``roots`` deletes from whose column
    leads no index.

    PostgreSQL checks each such FK per deleted row with
    ``WHERE <fk column> = $1``, RESTRICT and NO ACTION keys included; without
    a leading index that is a sequential scan of the referencing table for
    every purged message, thread or document (GOO-655 audit MS-1, GOO-737)."""
    reached, _ = _walk(metadata, roots)
    missing: list[str] = []
    for table in metadata.tables.values():
        leading = _leading_index_columns(table)
        for fk in table.foreign_keys:
            if fk.column.table.name in reached and fk.parent.name not in leading:
                missing.append(f"{table.name}.{fk.parent.name} -> {fk.target_fullname}")
    return sorted(missing)


def test_every_fk_a_thread_delete_reaches_is_indexed() -> None:
    assert _unindexed() == []


def test_the_index_check_reports_an_unindexed_fk() -> None:
    # Guards the guard: an FK into chat_messages with no index is reported,
    # and the same FK with index=True is not.
    metadata = MetaData()
    Table("threads", metadata, Column("id", Integer, primary_key=True))
    Table("chat_messages", metadata, Column("id", Integer, primary_key=True))
    Table(
        "notes",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("message_id", ForeignKey("chat_messages.id", ondelete="SET NULL")),
        Column("thread_id", ForeignKey("threads.id", ondelete="CASCADE"), index=True),
    )
    assert _unindexed(metadata) == ["notes.message_id -> chat_messages.id"]


def test_a_partial_or_non_leading_index_does_not_count() -> None:
    # agent_runs.thread_id had only the partial uq_agent_runs_active_thread,
    # and chat_messages.parent_message_id was second in ix_chat_messages_parent:
    # neither serves the per-row RI check, so both must be reported.
    metadata = MetaData()
    Table("threads", metadata, Column("id", Integer, primary_key=True))
    Table("chat_messages", metadata, Column("id", Integer, primary_key=True))
    runs = Table(
        "runs",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("thread_id", ForeignKey("threads.id", ondelete="SET NULL")),
        Column("active", Integer),
    )
    Index(
        "uq_runs_active_thread",
        runs.c.thread_id,
        unique=True,
        postgresql_where=runs.c.active == 1,
    )
    notes = Table(
        "notes",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("thread_id", ForeignKey("threads.id", ondelete="CASCADE"), index=True),
        Column("message_id", ForeignKey("chat_messages.id", ondelete="SET NULL")),
    )
    Index("ix_notes_thread_message", notes.c.thread_id, notes.c.message_id)

    assert _unindexed(metadata) == [
        "notes.message_id -> chat_messages.id",
        "runs.thread_id -> threads.id",
    ]


def test_every_fk_a_document_delete_reaches_is_indexed() -> None:
    # Only the index rule applies here: a RESTRICT key into documents (research
    # evidence, full-text attempts) is meant to refuse the delete, but its
    # check still runs per deleted row (GOO-737).
    assert _unindexed(roots=DOCUMENT_DELETE) == []


def test_the_document_walk_follows_cascades() -> None:
    # Guards the guard: a walk that stopped at documents would never check the
    # FKs into the rows its CASCADEs delete.
    reached, _ = _walk(roots=DOCUMENT_DELETE)
    assert {"entities", "entity_relationships", "search_results"} <= reached


def test_the_document_index_check_ignores_a_partial_index() -> None:
    # extraction_accepted_values.document_id led only the partial
    # uq_extraction_accepted_initial; it must be reported, and the same FK
    # reached through a CASCADE child must be reported as well.
    metadata = MetaData()
    Table("documents", metadata, Column("id", Integer, primary_key=True))
    Table(
        "entities",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("document_id", ForeignKey("documents.id", ondelete="CASCADE")),
    )
    accepted = Table(
        "accepted",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("document_id", ForeignKey("documents.id", ondelete="RESTRICT")),
        Column("entity_id", ForeignKey("entities.id", ondelete="RESTRICT")),
        Column("initial", Integer),
    )
    Index(
        "uq_accepted_initial",
        accepted.c.document_id,
        unique=True,
        postgresql_where=accepted.c.initial == 1,
    )

    assert _unindexed(metadata, DOCUMENT_DELETE) == [
        "accepted.document_id -> documents.id",
        "accepted.entity_id -> entities.id",
        "entities.document_id -> documents.id",
    ]


def test_agent_runs_conversation_and_project_fks_are_indexed() -> None:
    # Off the document path: deleting a conversation or a collection SET NULLs
    # these per row (GOO-737). The rest of the collection delete path is not
    # held to the index rule yet.
    leading = _leading_index_columns(Base.metadata.tables["agent_runs"])
    assert {"conversation_id", "project_id"} <= leading
