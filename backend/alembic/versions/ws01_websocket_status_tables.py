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

Offline (``--sql``): an explicit ``FROM:TO`` range renders the DDL for all
four tables. A from-empty render (no start revision) emits nothing here,
because r6h3's offline ``create_all`` already rendered them.

downgrade() is a deliberate no-op. These tables may predate this revision
(create_all-provisioned environments), and dropping them would destroy live
connection and status history this revision never created.
"""

import logging

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


def _model_tables() -> list:
    # Same registration contract as r6h3_model_baseline / alembic/env.py.
    import src.models  # noqa: F401
    from src.models.base import Base

    return [Base.metadata.tables[name] for name in TABLES]


def upgrade() -> None:
    from src.models.base import Base

    tables = _model_tables()
    bind = op.get_bind()

    # op.get_context().as_sql == offline mode; unlike context.is_offline_mode()
    # it also works when upgrade() runs under a bare Operations context.
    if op.get_context().as_sql:
        if context.get_starting_revision_argument() in (None, "base"):
            return  # r6h3_model_baseline already rendered them
        Base.metadata.create_all(bind=bind, tables=tables, checkfirst=False)
        return

    existing = set(sa.inspect(bind).get_table_names())
    missing = [table for table in tables if table.name not in existing]
    if not missing:
        return
    logging.getLogger("alembic").info(
        "GOO-686: creating missing realtime status tables: %s",
        ", ".join(table.name for table in missing),
    )
    # create_all orders the tables by foreign key and creates their enum types.
    Base.metadata.create_all(bind=bind, tables=missing, checkfirst=True)


def downgrade() -> None:
    # Intentional no-op; see the module docstring.
    pass
