"""Create the realtime/WebSocket status tables where they are missing (GOO-686).

Revision ID: ws01_websocket_status_tables
Revises: rl01_secure_late_internal_rls
Create Date: 2026-11-22

``src/models/websocket_status.py`` maps four tables: ``websocket_connections``,
``connection_events``, ``status_updates`` and ``notification_templates``. They
back mounted routes (``/api/v2/realtime/*``, ``/api/v2/ws/connect``) and the
WebSocket service initializer that runs at every boot. Until GOO-686,
``src.models`` never imported that module, so the tables were absent from
``Base.metadata`` wherever alembic looked:

* ``r6h3_model_baseline`` (``Base.metadata.create_all``) never created them;
* the blocking ``alembic check`` drift gate never compared them;
* managed databases skip boot-time ``create_all`` (``main.py``), so an
  alembic-built database lacks them, and the reads (``select(StatusUpdate)``)
  and writes against them fail there.

GOO-686 registers the module in ``src.models``. That covers fresh databases
through the r6h3 baseline. This revision backfills databases that already
passed r6h3. Following r6h3, the ORM models stay the single source of truth:
it calls ``create_all`` for just these four tables and only creates the ones
that are missing. Existing tables, from a create_all-provisioned environment
or a manual fix, are left untouched, so the revision is safe to run anywhere
and a rerun is a no-op.

Security: all four tables are locked down the ``h8i9j0k1l2m3`` /
``rl01_secure_late_internal_rls`` way, whether this revision created them or
they already existed. RLS is enabled with no client policies, and ALL is
revoked from the PostgREST roles ``anon`` / ``authenticated`` (each only if
the role exists). These are backend-only tables reached over the direct
postgres connection; ``websocket_connections`` holds ``user_id``,
``ip_address`` and ``user_agent``. Foreign keys keep the models' ``ondelete``
behaviour as declared.

Fail-loud guards, run BEFORE any DDL (PostgreSQL only):

* A pre-existing enum type named ``priority`` / ``updatetype`` /
  ``connectionstatus`` must have exactly the model's labels, in order.
  ``create_all(checkfirst=True)`` would otherwise reuse a foreign type, and
  inserts would fail later.
* A pre-existing ``websocket_connections`` with the legacy Supabase shape
  (``supabase/migrations/20260303204052_initial_schema.sql``: no
  ``organization_id`` / ``connection_status``) stops the upgrade with a
  clear message instead of being silently kept. It needs a manual decision:
  rename it, or migrate its rows.

Offline (``--sql``): the guards and the RLS lockdown render as the same
``DO`` blocks the online run executes. CREATE TABLE renders for an explicit
``FROM:TO`` range. A from-empty render (no start revision) skips the CREATEs,
because r6h3's offline ``create_all`` already rendered them, but still
renders the guards and lockdown. Unlike online, the offline CREATEs assume
the tables are absent, since offline mode cannot inspect the database.

downgrade() is a deliberate no-op. These tables may predate this revision
(create_all-provisioned environments), and dropping them would destroy live
connection and status history this revision never created.
"""

import logging
from typing import Dict, List, Sequence

import sqlalchemy as sa
from alembic import context, op

revision = "ws01_websocket_status_tables"
down_revision = "rl01_secure_late_internal_rls"
branch_labels = None
depends_on = None

TABLES = (
    "websocket_connections",
    "connection_events",
    "status_updates",
    "notification_templates",
)


def _model_tables() -> List[sa.Table]:
    # Same registration contract as r6h3_model_baseline / alembic/env.py.
    import src.models  # noqa: F401
    from src.models.base import Base

    return [Base.metadata.tables[name] for name in TABLES]


_POSTGREST_ROLES = ("anon", "authenticated")


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
            SELECT array_agg(e.enumlabel::text ORDER BY e.enumsortorder) INTO found
                FROM pg_enum e WHERE e.enumtypid = 'public.{name}'::regtype;
            IF kind <> 'e' OR found IS DISTINCT FROM {expected} THEN
                RAISE EXCEPTION 'GOO-686: existing enum type public.{name} has labels %, expected %; the realtime status models cannot use it. Fix or rename the type by hand before upgrading.', found, {expected};
            END IF;
        END
        $$
        """


_LEGACY_SHAPE_GUARD = """
    DO $$
    BEGIN
        IF to_regclass('public.websocket_connections') IS NOT NULL AND (
            NOT EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'websocket_connections'
                AND column_name = 'organization_id')
            OR NOT EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'websocket_connections'
                AND column_name = 'connection_status')
        ) THEN
            RAISE EXCEPTION 'GOO-686: public.websocket_connections has the legacy Supabase shape (no organization_id / connection_status), which the WebSocketConnection model cannot use. Rename or migrate it by hand, then rerun the upgrade.';
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
    from src.models.base import Base

    tables = _model_tables()
    bind = op.get_bind()
    # op.get_context().as_sql == offline mode; unlike context.is_offline_mode()
    # it also works when upgrade() runs under a bare Operations context.
    offline = bool(op.get_context().as_sql)
    postgres = offline or bind.dialect.name == "postgresql"

    if postgres:
        for name, labels in _enum_types(tables).items():
            op.execute(_enum_guard(name, labels))
        op.execute(_LEGACY_SHAPE_GUARD)

    if offline:
        if context.get_starting_revision_argument() not in (None, "base"):
            Base.metadata.create_all(bind=bind, tables=tables, checkfirst=False)
        # else: r6h3_model_baseline already rendered the CREATEs.
    else:
        existing = set(sa.inspect(bind).get_table_names())
        missing = [table for table in tables if table.name not in existing]
        if missing:
            logging.getLogger("alembic").info(
                "GOO-686: creating missing realtime status tables: %s",
                ", ".join(table.name for table in missing),
            )
            # create_all orders tables by foreign key and creates enum types.
            Base.metadata.create_all(bind=bind, tables=missing, checkfirst=True)

    if postgres:
        # Created now or pre-existing: never leave one reachable by PostgREST.
        for table in TABLES:
            op.execute(_lockdown(table))


def downgrade() -> None:
    # Intentional no-op; see the module docstring.
    pass
