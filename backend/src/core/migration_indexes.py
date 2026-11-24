"""PostgreSQL index helpers for Alembic migrations."""

import alembic.op as op

_USER_MESSAGE_INDEX = "uq_chat_messages_thread_client_msg_user"


def require_valid_index(table_name: str, index_name: str) -> None:
    """Raise if the named index on the target table is missing or invalid."""
    # Use revision-defined names. Running the check in SQL also preserves it
    # when Alembic generates an offline upgrade script.
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_index i
                JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
                WHERE i.indrelid = pg_catalog.to_regclass('{table_name}')
                  AND c.relname = '{index_name}'
                  AND i.indisvalid
            ) THEN
                RAISE EXCEPTION 'Expected valid index {index_name} on {table_name}';
            END IF;
        END
        $$;
        """)


def ensure_user_message_index() -> None:
    """Repair an invalid user-message index left by an interrupted build."""
    op.execute(f"""
        DO $$
        DECLARE
            invalid_index regclass;
        BEGIN
            SELECT i.indexrelid INTO invalid_index
            FROM pg_catalog.pg_index i
            JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
            WHERE i.indrelid = pg_catalog.to_regclass('chat_messages')
              AND c.relname = '{_USER_MESSAGE_INDEX}'
              AND NOT i.indisvalid;

            IF invalid_index IS NOT NULL THEN
                EXECUTE 'DROP INDEX ' || invalid_index;
            END IF;
        END
        $$;
        """)

    with op.get_context().autocommit_block():
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            f"{_USER_MESSAGE_INDEX} "
            "ON chat_messages (thread_id, client_message_id) "
            "WHERE client_message_id IS NOT NULL AND role = 'user'"
        )
    require_valid_index("chat_messages", _USER_MESSAGE_INDEX)
