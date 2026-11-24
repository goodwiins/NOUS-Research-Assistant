"""Repair an INVALID uq_chat_messages_thread_client_msg_user index

Revision ID: repair_invalid_chat_msg_idx
Revises: ensure_upsert_uq_constraints
Create Date: 2026-07-01 12:00:00

``uq_chat_messages_thread_client_msg_user`` is created with
``CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS`` (in ``v0a1b2c3d4e5`` and
``ensure_upsert_uq_constraints``). If a concurrent build is interrupted
(deploy kill, connection drop), Postgres leaves the index behind marked
``indisvalid = false``: it is not used by queries and cannot be inferred by
``ON CONFLICT`` — yet ``IF NOT EXISTS`` sees the name and silently skips
recreation on every later run. The idempotency upsert in
``jobs._persist_user_message`` would then 500 forever with
``InvalidColumnReferenceError`` while the migrations report success.

This migration drops the index iff it exists AND is invalid, then recreates it
concurrently. No-op when the index is valid (the common case). Self-healing:
if this migration's own concurrent build is interrupted, re-running it drops
the invalid leftover and tries again.
"""

from src.core.migration_indexes import ensure_user_message_index

revision = "repair_invalid_chat_msg_idx"
down_revision = "ensure_upsert_uq_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_user_message_index()


def downgrade() -> None:
    # Repair-only migration: the index is owned by v0a1b2c3d4e5 /
    # ensure_upsert_uq_constraints, so downgrade must not drop it.
    pass
