"""GOO-761: ``/search/analytics`` reports an org with no timed search as
null, not 0 ms (the frontend shows "—" for null since #2303)."""

from __future__ import annotations

from typing import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.core.database as database
from src.api.search import search as search_api
from src.models.search_analytics import SearchAnalyticsEvent
from src.models.search_schemas import SearchAnalytics

pytestmark = pytest.mark.unit


@pytest.fixture
def org_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SearchAnalyticsEvent.__table__.create(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    try:
        yield factory
    finally:
        engine.dispose()


def test_an_org_without_searches_has_no_average_time(org_db: sessionmaker) -> None:
    result = search_api._load_search_analytics(str(uuid4()), 30)

    assert result["total_searches"] == 0
    assert result["average_search_time_ms"] is None
    assert SearchAnalytics(**result).average_search_time_ms is None


def test_timed_searches_still_average(org_db: sessionmaker) -> None:
    org = uuid4()
    with org_db() as db:
        for ms in (100.0, 300.0):
            db.add(
                SearchAnalyticsEvent(
                    search_id=str(uuid4()),
                    query="q",
                    organization_id=org,
                    result_count=1,
                    search_time_ms=ms,
                )
            )
        db.commit()

    result = search_api._load_search_analytics(str(org), 30)

    assert result["average_search_time_ms"] == pytest.approx(200.0)
