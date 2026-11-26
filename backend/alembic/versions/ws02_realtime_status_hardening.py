"""Harden the realtime/WebSocket status tables after ws01 (GOO-690).

Revision ID: ws02_realtime_status_hardening
Revises: ws01_websocket_status_tables
Create Date: 2026-11-23

``ws01_websocket_status_tables`` (GOO-686) is already applied on dev, so it is
not edited. This follow-up carries the #2229 review fixes as a new revision.
Like ws01 it reads the four tables from the ORM models
(``src/models/websocket_status.py``), and it is safe to run anywhere: a rerun
is a no-op.

1. Fail-loud guards, run BEFORE any DDL (PostgreSQL only):

   * A pre-existing type named ``priority`` / ``updatetype`` /
     ``connectionstatus`` must be an enum (otherwise the error says it isn't)
     with exactly the model's labels, in order. ws01 reported a non-enum type
     as ``labels <NULL>``.
   * Each of the four tables, if it exists, must have every column its model
     declares. Otherwise the error names the missing columns and gives the
     ``information_schema`` query to inspect the table. ws01 checked only two
     columns of ``websocket_connections`` (the legacy Supabase shape from
     ``supabase/migrations/20260303204052_initial_schema.sql``).

2. Ensure-create: any of the four tables that is missing is created, with
   its indexes and, if missing, its own three enum types. Nothing else is
   created. ws01 used ``metadata.create_all(tables=...)``, which also creates
   every other missing enum type the metadata knows; on a database that has
   already run ws01 that cannot be undone here, and this revision does not
   repeat it.

   * PostgreSQL, online and offline (``--sql``), renders the same idempotent
     ``DO`` blocks: ``CREATE TYPE`` only if ``to_regtype`` is NULL, and
     ``CREATE TABLE`` plus its ``CREATE INDEX`` only if ``to_regclass`` is
     NULL, tables in FK order. So the offline render is correct whatever
     state the target database is in.
   * Other dialects (the SQLite unit tests): ``Table.create(checkfirst=True)``
     per missing table.

3. Lockdown, the ``rl01_secure_late_internal_rls`` way, whether a table was
   created now or already existed: RLS enabled with no client policies, and
   ALL revoked from the PostgREST roles ``anon`` / ``authenticated`` (each
   only if the role exists). ``websocket_connections`` holds ``user_id``,
   ``ip_address`` and ``user_agent``.

downgrade() is a deliberate no-op, like ws01's: the tables may predate both
revisions, and dropping them would destroy live connection and status history.
"""

import logging
from typing import Dict, List, Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ws02_realtime_status_hardening"
down_revision = "ws01_websocket_status_tables"
branch_labels = None
depends_on = None

TABLES = (
    "websocket_connections",
    "connection_events",
    "status_updates",
    "notification_templates",
)

_POSTGREST_ROLES = ("anon", "authenticated")


def _model_tables() -> List[sa.Table]:
    # Same registration contract as r6h3_model_baseline / alembic/env.py.
    import src.models  # noqa: F401
    from src.models.base import Base

    return [Base.metadata.tables[name] for name in TABLES]


def _sql_list(values: Sequence[str]) -> str:
    return "ARRAY[" + ", ".join(f"'{v}'" for v in values) + "]::text[]"


def _enum_types(tables: List[sa.Table]) -> Dict[str, List[str]]:
    """Native PG enum type name -> labels in order, as the models declare them."""
    found: Dict[str, List[str]] = {}
    for table in tables:
        for column in table.columns:
            if isinstance(column.type, sa.Enum) and column.type.native_enum:
                found[str(column.type.name)] = list(column.type.enums)
    return found


def _enum_guard(name: str, labels: List[str]) -> str:
    expected = _sql_list(labels)
    return f"""
        DO $$
        DECLARE
            kind "char";
            found text[];
        BEGIN
            SELECT t.typtype INTO kind FROM pg_type t
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE n.nspname = 'public' AND t.typname = '{name}';
            IF kind IS NULL THEN
                RETURN;
            END IF;
            IF kind <> 'e' THEN
                RAISE EXCEPTION 'GOO-690: public.{name} exists but is not an enum type (pg_type.typtype = %); the realtime status models need an enum with labels %. Rename or drop it by hand before upgrading.', kind, {expected};
            END IF;
            SELECT array_agg(e.enumlabel::text ORDER BY e.enumsortorder) INTO found
                FROM pg_enum e WHERE e.enumtypid = 'public.{name}'::regtype;
            IF found IS DISTINCT FROM {expected} THEN
                RAISE EXCEPTION 'GOO-690: existing enum type public.{name} has labels %, expected %; the realtime status models cannot use it. Fix or rename the type by hand before upgrading.', found, {expected};
            END IF;
        END
        $$
        """


def _column_guard(table: sa.Table) -> str:
    """Fail if a pre-existing ``table`` lacks any column its model declares."""
    expected = _sql_list([column.name for column in table.columns])
    diagnose = (
        "SELECT column_name, data_type FROM information_schema.columns "
        f"WHERE table_schema = ''public'' AND table_name = ''{table.name}'' "
        "ORDER BY ordinal_position"
    )
    return f"""
        DO $$
        DECLARE
            missing text[];
        BEGIN
            IF to_regclass('public."{table.name}"') IS NULL THEN
                RETURN;
            END IF;
            SELECT array_agg(c ORDER BY c) INTO missing
                FROM unnest({expected}) AS c
                WHERE NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = '{table.name}'
                    AND column_name = c
                );
            IF missing IS NOT NULL THEN
                RAISE EXCEPTION 'GOO-690: existing public.{table.name} is missing column(s) % that its ORM model needs (for websocket_connections this is the legacy Supabase shape). Inspect it with: {diagnose}; then rename or migrate the table by hand and rerun the upgrade.', missing;
            END IF;
        END
        $$
        """


def _ensure_enum(name: str, labels: List[str]) -> str:
    values = ", ".join(f"'{v}'" for v in labels)
    return f"""
        DO $$
        BEGIN
            IF to_regtype('public.{name}') IS NULL THEN
                CREATE TYPE public.{name} AS ENUM ({values});
            END IF;
        END
        $$
        """


def _ensure_table(table: sa.Table) -> str:
    """Create ``table`` and its indexes only if the table is missing."""
    dialect = postgresql.dialect()
    ddl = [str(sa.schema.CreateTable(table).compile(dialect=dialect)).strip()]
    for index in sorted(table.indexes, key=lambda i: str(i.name)):
        ddl.append(str(sa.schema.CreateIndex(index).compile(dialect=dialect)).strip())
    for statement in ddl:
        if "$ddl$" in statement:  # pragma: no cover - would break the quoting
            raise ValueError(f"unexpected $ddl$ in DDL for {table.name}")
    executes = "\n".join(f"EXECUTE $ddl${statement}$ddl$;" for statement in ddl)
    return f"""
        DO $$
        BEGIN
            IF to_regclass('public."{table.name}"') IS NULL THEN
                {executes}
                RAISE NOTICE 'GOO-690: created missing table public.{table.name}';
            END IF;
        END
        $$
        """


def _lockdown(table: str) -> str:
    """rl01_secure_late_internal_rls's block, verbatim in shape."""
    revokes = "\n".join(f"""
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    EXECUTE 'REVOKE ALL ON TABLE public."{table}" FROM {role}';
                END IF;""" for role in _POSTGREST_ROLES)
    return f"""
        DO $$
        BEGIN
            IF to_regclass('public."{table}"') IS NULL THEN
                RAISE NOTICE 'skipping RLS for missing table public.{table}';
                RETURN;
            END IF;
            EXECUTE 'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY';{revokes}
        END
        $$
        """


def upgrade() -> None:
    tables = _model_tables()
    bind = op.get_bind()
    # op.get_context().as_sql == offline mode; unlike context.is_offline_mode()
    # it also works when upgrade() runs under a bare Operations context.
    offline = bool(op.get_context().as_sql)
    postgres = offline or bind.dialect.name == "postgresql"

    if not postgres:
        existing = set(sa.inspect(bind).get_table_names())
        missing = [table for table in tables if table.name not in existing]
        if missing:
            logging.getLogger("alembic").info(
                "GOO-690: creating missing realtime status tables: %s",
                ", ".join(table.name for table in missing),
            )
            for table in sa.schema.sort_tables(missing):
                table.create(bind=bind, checkfirst=True)
        return

    for name, labels in _enum_types(tables).items():
        op.execute(_enum_guard(name, labels))
    for table in tables:
        op.execute(_column_guard(table))

    for name, labels in _enum_types(tables).items():
        op.execute(_ensure_enum(name, labels))
    for table in sa.schema.sort_tables(tables):
        op.execute(_ensure_table(table))

    # Created now or pre-existing: never leave one reachable by PostgREST.
    for table_name in TABLES:
        op.execute(_lockdown(table_name))


def downgrade() -> None:
    # Intentional no-op; see the module docstring.
    pass
