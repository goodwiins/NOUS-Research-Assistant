"""Cross-org isolation for behavior analytics (review of GOO-644 / #2207).

Once ``POST /events`` works, a client-supplied ``search_query_id`` (no FK on
``search_events``) could point an org-B event at an org-A search query, and
org-A content-usage analytics joined events only through ``search_queries``,
so it counted org-B activity. Closed on both sides:

* write: ``track_search_event`` rejects a ``search_query_id`` that is malformed
  (422) or not in the caller's org (404), and the session lookup is scoped;
* read: ``_query_content_usage`` and the /insights top-documents query
  (``_query_top_documents``) filter events, sessions and queries by org.

The write-side service tests run against real tables (GOO-677): a mock that
returns a row for any filter can't catch a dropped org predicate.

``POST /sessions`` also returned another user's session for a reused token;
it now answers 409 instead.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.quality import user_behavior as ub
from src.models.quality_metrics import SearchEvent, SearchSession
from src.models.search import SearchQuery, SearchType
from src.models.user import UserRole
from src.schemas.quality_metrics import AnalyticsExportRequest
from src.services.quality import user_behavior_service as svc_mod
from src.services.quality.user_behavior_service import UserBehaviorService

pytestmark = pytest.mark.unit

ORG_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ORG_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
# Ids start with a letter on purpose: SQLite gives a column typed UUID numeric
# affinity, so an all-digit hex id would read back as a float.
USER_A = "a1111111-1111-1111-1111-111111111111"
USER_B = "b2222222-2222-2222-2222-222222222222"
QUERY_A = "aaaaaaaa-0000-0000-0000-00000000000a"
QUERY_B = "bbbbbbbb-0000-0000-0000-00000000000b"
EVENT: dict[str, Any] = {
    "session_id": "tok",
    "query": "q",
    "search_type": "fulltext",
    "results_count": 1,
    "response_time": 10.0,
}


def _caller(user: str, org: str) -> Any:
    return SimpleNamespace(id=user, role=UserRole.ADMIN, organization_id=org)


# ---- read side: real SQL against a minimal schema ---------------------------

_DDL = [
    "CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT, organization_id TEXT)",
    "CREATE TABLE search_queries (id TEXT PRIMARY KEY, organization_id TEXT)",
    "CREATE TABLE search_results (id TEXT PRIMARY KEY, document_id TEXT,"
    " search_query_id TEXT)",
    "CREATE TABLE search_sessions (id TEXT PRIMARY KEY, user_id TEXT,"
    " organization_id TEXT)",
    "CREATE TABLE search_events (id TEXT PRIMARY KEY, session_id TEXT,"
    " search_query_id TEXT, user_id TEXT, organization_id TEXT,"
    " clicked_results INTEGER, results_count INTEGER, created_at TIMESTAMP)",
]


@pytest.fixture
def db() -> Iterator[Session]:
    # One shared in-memory connection usable from any thread: the export route
    # runs _query_content_usage in a worker thread (asyncio.to_thread).
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        for ddl in _DDL:
            conn.execute(text(ddl))
    session = sessionmaker(bind=engine)()
    now = datetime.utcnow()
    rows: list[tuple[str, dict[str, Any]]] = [
        ("INSERT INTO documents VALUES ('doc-a', 'A paper', :a)", {}),
        ("INSERT INTO search_queries VALUES ('q-a', :a)", {}),
        ("INSERT INTO search_results VALUES ('r-a', 'doc-a', 'q-a')", {}),
        ("INSERT INTO search_sessions VALUES ('s-a', :ua, :a)", {}),
        ("INSERT INTO search_sessions VALUES ('s-b', :ub, :b)", {}),
        # one legitimate org-A event ...
        (
            "INSERT INTO search_events VALUES"
            " ('e-a', 's-a', 'q-a', :ua, :a, 1, 3, :t)",
            {},
        ),
        # ... and two org-B events forged onto org-A's search query
        (
            "INSERT INTO search_events VALUES"
            " ('e-b1', 's-b', 'q-a', :ub, :b, 1, 3, :t)",
            {},
        ),
        (
            "INSERT INTO search_events VALUES"
            " ('e-b2', 's-b', 'q-a', :ub, :b, 1, 3, :t)",
            {},
        ),
        # an older org-A document, used only by the export-window tests
        ("INSERT INTO documents VALUES ('doc-old', 'Old paper', :a)", {}),
        ("INSERT INTO search_queries VALUES ('q-old', :a)", {}),
        ("INSERT INTO search_results VALUES ('r-old', 'doc-old', 'q-old')", {}),
        (
            "INSERT INTO search_events VALUES"
            " ('e-old', 's-a', 'q-old', :ua, :a, 0, 2, :old)",
            {},
        ),
    ]
    params = {
        "a": ORG_A,
        "b": ORG_B,
        "ua": USER_A,
        "ub": USER_B,
        "t": now,
        "old": now - timedelta(days=20),
    }
    for sql, extra in rows:
        session.execute(text(sql), {**params, **extra})
    session.commit()
    yield session
    session.close()


def test_org_b_events_do_not_reach_org_a_content_usage(db: Session) -> None:
    rows = ub._query_content_usage(db, ORG_A, days_back=7, limit=10)

    assert len(rows) == 1
    row = rows[0]
    assert row["document_id"] == "doc-a"
    assert row["access_count"] == 1  # only USER_A, not the org-B user
    assert row["search_appearances"] == 1
    assert row["total_clicks"] == 1


def test_org_b_sees_nothing_of_org_a_documents(db: Session) -> None:
    assert ub._query_content_usage(db, ORG_B, days_back=7, limit=10) == []


def _top_documents(db: Session, org: str) -> list[Any]:
    now = datetime.utcnow()
    return svc_mod._query_top_documents(
        db, org, now - timedelta(days=7), now + timedelta(minutes=1)
    )


def test_insights_top_documents_ignore_forged_org_b_events(db: Session) -> None:
    rows = _top_documents(db, ORG_A)

    assert [(r.id, r.unique_users, r.access_count) for r in rows] == [
        ("doc-a", 1, 1)  # USER_A's one event, not the two forged org-B ones
    ]


def test_insights_top_documents_for_org_b_are_empty(db: Session) -> None:
    assert _top_documents(db, ORG_B) == []


# ---- write side: service, against real tables -------------------------------


@pytest.fixture
def orm_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    """Real search_sessions / search_queries / search_events tables, two orgs.

    Each org has its own session token and search query. ``get_db_sync`` hands
    the service a fresh session per call, like production.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models: tuple[Any, ...] = (SearchSession, SearchQuery, SearchEvent)
    for model in models:
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    with factory() as seed:
        for user, org, token in ((USER_A, ORG_A, "tok-a"), (USER_B, ORG_B, "tok-b")):
            seed.add(
                SearchSession(
                    session_id=token,
                    user_id=uuid.UUID(user),
                    organization_id=uuid.UUID(org),
                )
            )
        for qid, user, org in ((QUERY_A, USER_A, ORG_A), (QUERY_B, USER_B, ORG_B)):
            seed.add(
                SearchQuery(
                    id=uuid.UUID(qid),
                    query_text="q",
                    search_type=SearchType.KEYWORD,
                    user_id=uuid.UUID(user),
                    organization_id=uuid.UUID(org),
                )
            )
        seed.commit()

    def get_db_sync() -> Iterator[Session]:
        yield factory()

    monkeypatch.setattr(svc_mod, "get_db_sync", get_db_sync)
    yield factory
    engine.dispose()


def _track(token: str, user: str, org: str, query_id: str) -> Any:
    return UserBehaviorService().track_search_event(
        **{**EVENT, "session_id": token},
        user_id=user,
        organization_id=org,
        search_query_id=query_id,
    )


def _events(factory: sessionmaker[Session]) -> list[SearchEvent]:
    with factory() as s:
        return list(s.query(SearchEvent).all())


async def test_foreign_search_query_id_writes_nothing(
    orm_db: sessionmaker[Session],
) -> None:
    # Org B's own session, but org A's search query: must not be planted.
    event = await _track("tok-b", USER_B, ORG_B, QUERY_A)

    assert event is None
    assert _events(orm_db) == []
    with orm_db() as s:
        session = s.query(SearchSession).filter_by(session_id="tok-b").one()
        assert session.search_count == 0


async def test_own_search_query_id_is_recorded_in_own_org(
    orm_db: sessionmaker[Session],
) -> None:
    event = await _track("tok-b", USER_B, ORG_B, QUERY_B)

    assert event is not None
    [stored] = _events(orm_db)
    assert stored.search_query_id == uuid.UUID(QUERY_B)
    assert stored.organization_id == uuid.UUID(ORG_B)
    with orm_db() as s:
        session = s.query(SearchSession).filter_by(session_id="tok-b").one()
        assert stored.session_id == session.id
        assert session.search_count == 1


async def test_another_orgs_session_token_writes_nothing(
    orm_db: sessionmaker[Session],
) -> None:
    # Org B caller presenting org A's session token (and org A's query).
    assert await _track("tok-a", USER_B, ORG_B, QUERY_A) is None
    assert _events(orm_db) == []


async def test_malformed_search_query_id_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_db = MagicMock()
    monkeypatch.setattr(svc_mod, "get_db_sync", get_db)

    with pytest.raises(ValueError):
        await UserBehaviorService().track_search_event(
            **EVENT,
            user_id=USER_A,
            organization_id=ORG_A,
            search_query_id="not-a-uuid",
        )
    get_db.assert_not_called()


# ---- write side: routes -----------------------------------------------------


async def test_events_route_malformed_ids_are_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ub.user_behavior_service,
        "track_search_event",
        AsyncMock(side_effect=ValueError("badly formed hexadecimal UUID string")),
    )
    with pytest.raises(HTTPException) as ei:
        await ub.track_search_event(
            event_data={**EVENT, "search_query_id": "nope"},
            current_user=_caller(USER_A, ORG_A),
        )
    assert ei.value.status_code == 422
    assert "hexadecimal" not in str(ei.value.detail)  # no raw exception text


def _sessions_db(existing: Any) -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = existing
    return db


def test_create_session_reused_token_of_another_user_is_409() -> None:
    other = SimpleNamespace(
        session_id="tok",
        user_id=uuid.UUID(USER_B),
        organization_id=uuid.UUID(ORG_B),
    )
    db = _sessions_db(other)

    with pytest.raises(HTTPException) as ei:
        ub.create_search_session(
            session_data={"session_id": "tok"},
            current_user=_caller(USER_A, ORG_A),
            db=db,
        )
    assert ei.value.status_code == 409
    db.add.assert_not_called()


def test_create_session_returns_callers_own_existing_session() -> None:
    mine = SimpleNamespace(
        session_id="tok",
        user_id=uuid.UUID(USER_A),
        organization_id=uuid.UUID(ORG_A),
    )
    resp = ub.create_search_session(
        session_data={"session_id": "tok"},
        current_user=_caller(USER_A, ORG_A),
        db=_sessions_db(mine),
    )
    assert resp == {"session_id": "tok", "status": "existing"}


def test_create_session_missing_token_is_422() -> None:
    with pytest.raises(HTTPException) as ei:
        ub.create_search_session(
            session_data={},
            current_user=_caller(USER_A, ORG_A),
            db=_sessions_db(None),
        )
    assert ei.value.status_code == 422


def test_cutoff_uses_days_back() -> None:
    # Guard the window the read helper applies (shared by both routes).
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    ub._query_content_usage(db, ORG_A, days_back=3, limit=5)
    params = db.execute.call_args.args[1]
    assert params["org_id"] == ORG_A
    assert datetime.utcnow() - params["cutoff_date"] >= timedelta(days=3) - timedelta(
        seconds=5
    )


# ---- export honours its date window (GOO-677 / API2B-2) ---------------------


def _export(data_type: str, start: datetime, end: datetime, fmt: str) -> Any:
    return AnalyticsExportRequest(
        data_type=data_type,
        start_date=start,
        end_date=end,
        format=fmt,
        filters=None,
        include_metadata=True,
    )


async def test_export_content_usage_uses_start_and_end_date(db: Session) -> None:
    now = datetime.utcnow()
    resp = await ub.export_behavior_data(
        export_request=_export(
            "content_usage", now - timedelta(days=30), now - timedelta(days=10), "json"
        ),
        background_tasks=BackgroundTasks(),
        current_user=_caller(USER_A, ORG_A),
        db=db,
    )

    # Only the 20-day-old event is inside [start, end); today's doc-a is not.
    assert [r["document_id"] for r in resp["data"]["content_usage"]] == ["doc-old"]
    assert resp["format"] == "json"


async def test_export_content_usage_as_csv(db: Session) -> None:
    now = datetime.utcnow()
    resp = await ub.export_behavior_data(
        export_request=_export(
            "content_usage", now - timedelta(days=30), now - timedelta(days=10), "csv"
        ),
        background_tasks=BackgroundTasks(),
        current_user=_caller(USER_A, ORG_A),
        db=db,
    )

    assert resp["format"] == "csv"
    rows = list(csv.DictReader(io.StringIO(resp["data"]["content_usage"])))
    assert [r["document_id"] for r in rows] == ["doc-old"]
    assert rows[0]["title"] == "Old paper"
