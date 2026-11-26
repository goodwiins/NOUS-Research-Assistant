"""Real-Postgres proof that /insights and trends ignore another org's events
(GOO-691).

``search_events.session_id`` has no tenancy check of its own, so an org-B
event can sit on an org-A session. The engagement, query, search-type, hour,
satisfaction and completion queries joined events only through the session's
org, so that event counted for org A. They now also filter
``e.organization_id``; for the LEFT JOINs the predicate is in the ON clause,
so an org-A session without events still counts.

The queries use Postgres-only SQL (DATE_TRUNC, EXTRACT), so this runs only
with ``ORCHESTRATION_TEST_DATABASE_URL``; each test owns a throwaway schema.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from src.services.quality import user_behavior_service as svc_mod
from src.services.quality.user_behavior_service import UserBehaviorService

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

ORG_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ORG_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

_DDL = [
    "CREATE TABLE users (id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT,"
    " organization_id TEXT)",
    "CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT, organization_id TEXT)",
    "CREATE TABLE search_queries (id TEXT PRIMARY KEY, organization_id TEXT)",
    "CREATE TABLE search_results (id TEXT PRIMARY KEY, document_id TEXT,"
    " search_query_id TEXT)",
    "CREATE TABLE search_sessions (id TEXT PRIMARY KEY, user_id TEXT,"
    " organization_id TEXT, start_time TIMESTAMP, end_time TIMESTAMP,"
    " search_count INTEGER)",
    "CREATE TABLE search_events (id TEXT PRIMARY KEY, session_id TEXT,"
    " search_query_id TEXT, user_id TEXT, organization_id TEXT, query TEXT,"
    " search_type TEXT, response_time FLOAT, clicked_results INTEGER,"
    " results_count INTEGER, user_rating FLOAT, created_at TIMESTAMP)",
]
_ROWS = [
    "INSERT INTO users VALUES ('ua', 'Ada', 'A', :a), ('ua2', 'Al', 'A', :a),"
    " ('ub', 'Bo', 'B', :b)",
    "INSERT INTO documents VALUES ('doc-a', 'A paper', :a)",
    "INSERT INTO search_queries VALUES ('q-a', :a)",
    "INSERT INTO search_results VALUES ('r-a', 'doc-a', 'q-a')",
    "INSERT INTO search_sessions VALUES"
    " ('s-a', 'ua', :a, :t, :t + interval '60 seconds', 1),"
    # an org-A session with no events: the LEFT JOINs must keep it
    " ('s-empty', 'ua2', :a, :t, :t + interval '30 seconds', 0)",
    "INSERT INTO search_events VALUES"
    " ('e-a', 's-a', 'q-a', 'ua', :a, 'alpha', 'fulltext', 100, 1, 3, 4, :t),"
    # an org-B event forged onto org-A's session
    " ('e-x', 's-a', 'q-a', 'ub', :b, 'forged', 'forged_type', 9000, 0, 0, 1, :t)",
]


def _dsn() -> str:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg2://" + dsn[len(prefix) :]
    raise ValueError("ORCHESTRATION_TEST_DATABASE_URL must be a PostgreSQL URL")


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    schema = "behavior_insights_" + uuid.uuid4().hex
    admin = create_engine(_dsn(), poolclass=NullPool)
    with admin.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(
        _dsn(),
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    try:
        with engine.begin() as conn:
            for ddl in _DDL:
                conn.execute(text(ddl))
            params: dict[str, Any] = {
                "a": ORG_A,
                "b": ORG_B,
                "t": datetime.utcnow() - timedelta(hours=1),
            }
            for sql in _ROWS:
                conn.execute(text(sql), params)
        factory = sessionmaker(bind=engine)

        def get_db_sync() -> Iterator[Session]:
            yield factory()

        monkeypatch.setattr(svc_mod, "get_db_sync", get_db_sync)
        yield
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


async def test_insights_count_only_the_orgs_own_events(seeded: None) -> None:
    insights = await UserBehaviorService().get_behavioral_insights(ORG_A)

    assert insights["active_users"] == 2
    assert insights["total_sessions"] == 2  # s-a once, s-empty kept
    assert [q["query"] for q in insights["top_queries"]] == ["alpha"]
    assert set(insights["search_types_distribution"]) == {"fulltext"}
    assert insights["user_satisfaction"] == pytest.approx(4.0)
    assert insights["task_completion_rate"] == pytest.approx(100.0)
    # Averaged over the LEFT-joined rows: e-a clicked, s-empty has no event
    # (0). The forged unclicked event would make it 1/3.
    assert insights["click_through_rates"]["overall"] == pytest.approx(0.5)
    assert [
        (d["document_id"], d["access_count"])
        for d in insights["most_accessed_documents"]
    ] == [("doc-a", 1)]


async def test_engagement_trends_count_only_the_orgs_own_events(
    seeded: None,
) -> None:
    trends = await UserBehaviorService().get_behavior_trends(ORG_A, days_back=7)

    [engagement] = trends["engagement_trends"]
    assert engagement["active_users"] == 2  # s-empty's user kept
    assert engagement["avg_satisfaction"] == pytest.approx(4.0)


async def test_an_untimed_search_type_reports_no_response_time(
    seeded: None,
) -> None:
    """GOO-761: no measurement is null, not 0 seconds."""
    factory = svc_mod.get_db_sync
    db = next(factory())
    try:
        db.execute(
            text(
                "INSERT INTO search_events VALUES ('e-untimed', 's-a', 'q-a', 'ua',"
                " :a, 'beta', 'semantic', NULL, 0, 1, NULL, :t)"
            ),
            {"a": ORG_A, "t": datetime.utcnow() - timedelta(hours=1)},
        )
        db.commit()
    finally:
        db.close()

    insights = await UserBehaviorService().get_behavioral_insights(ORG_A)
    types = insights["search_types_distribution"]
    assert types["semantic"]["avg_response_time"] is None
    assert types["fulltext"]["avg_response_time"] == pytest.approx(100.0)


async def test_a_period_without_timed_searches_reports_no_response_time(
    seeded: None,
) -> None:
    factory = svc_mod.get_db_sync
    db = next(factory())
    try:
        db.execute(text("UPDATE search_events SET response_time = NULL"))
        db.commit()
    finally:
        db.close()

    trends = await UserBehaviorService().get_behavior_trends(ORG_A, days_back=7)
    [search] = trends["search_trends"]
    assert search["avg_response_time"] is None
