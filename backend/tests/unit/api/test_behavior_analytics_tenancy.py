"""Cross-org isolation for behavior analytics (review of GOO-644 / #2207).

Once ``POST /events`` works, a client-supplied ``search_query_id`` (no FK on
``search_events``) could point an org-B event at an org-A search query, and
org-A content-usage analytics joined events only through ``search_queries``,
so it counted org-B activity. Closed on both sides:

* write: ``track_search_event`` rejects a ``search_query_id`` that is malformed
  (422) or not in the caller's org (404), and the session lookup is scoped;
* read: ``_query_content_usage`` filters events, sessions and queries by org.

``POST /sessions`` also returned another user's session for a reused token;
it now answers 409 instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from src.api.quality import user_behavior as ub
from src.models.quality_metrics import SearchSession
from src.models.search import SearchQuery
from src.models.user import UserRole
from src.services.quality import user_behavior_service as svc_mod
from src.services.quality.user_behavior_service import UserBehaviorService

pytestmark = pytest.mark.unit

ORG_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ORG_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"
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
    engine = create_engine("sqlite://")
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
    ]
    params = {"a": ORG_A, "b": ORG_B, "ua": USER_A, "ub": USER_B, "t": now}
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


# ---- write side: service ----------------------------------------------------


def _db_for(session: Any, query: Any) -> MagicMock:
    def by_entity(entity: Any) -> MagicMock:
        found = {SearchSession: session, SearchQuery: query}[entity]
        chain = MagicMock()
        chain.filter.return_value.first.return_value = found
        return chain

    db = MagicMock()
    db.query.side_effect = by_entity
    return db


async def test_foreign_search_query_id_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(id=uuid.uuid4(), search_count=0, total_response_time=0)
    db = _db_for(session, None)  # query id not found in the caller's org
    monkeypatch.setattr(svc_mod, "get_db_sync", lambda: iter([db]))

    event = await UserBehaviorService().track_search_event(
        **EVENT,
        user_id=USER_B,
        organization_id=ORG_B,
        search_query_id=str(uuid.uuid4()),
    )

    assert event is None
    db.add.assert_not_called()
    db.commit.assert_not_called()


async def test_own_search_query_id_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(id=uuid.uuid4(), search_count=0, total_response_time=0)
    query_id = uuid.uuid4()
    db = _db_for(session, SimpleNamespace(id=query_id))
    monkeypatch.setattr(svc_mod, "get_db_sync", lambda: iter([db]))

    event = await UserBehaviorService().track_search_event(
        **EVENT,
        user_id=USER_A,
        organization_id=ORG_A,
        search_query_id=str(query_id),
    )

    assert event is not None and event.search_query_id == query_id


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
