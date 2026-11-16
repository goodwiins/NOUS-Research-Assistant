"""GOO-497: the KG search adapter honours ``offset`` and reports a real total.

``KnowledgeGraphService.search`` read ``offset`` from the request only to echo
it back: ``search_entities`` had no SKIP, so every page returned the first
``limit`` matches, and ``total_results`` was ``len(results)`` with
``has_more=False``, so a full page never offered a next one.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Iterator, List
from unittest.mock import MagicMock

import pytest

from src.models.graph import EntityType
from src.services.knowledge_graph.knowledge_graph_service import KnowledgeGraphService

pytestmark = pytest.mark.unit


def _entity(entity_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=entity_id,
        name=f"Entity {entity_id}",
        entity_type=EntityType.PERSON,
        confidence_score=0.9,
        context="ctx",
        source_document_id=f"doc-{entity_id}",
        created_at=datetime(2026, 1, 1),
        updated_at=None,
    )


def _svc(
    monkeypatch: pytest.MonkeyPatch, page: List[SimpleNamespace], total: int | None
) -> tuple:
    svc = KnowledgeGraphService()
    calls: dict = {}

    def _search_entities(query: Any, **kwargs: Any) -> Any:
        calls["search"] = kwargs
        return page

    def _count(query: Any, **kwargs: Any) -> int:
        if total is None:
            raise AssertionError("count query must not run for a short page")
        calls["count"] = kwargs
        return total

    monkeypatch.setattr(svc, "search_entities", _search_entities)
    monkeypatch.setattr(svc, "count_search_entities", _count)
    return svc, calls


def _search(svc: KnowledgeGraphService, limit: int, offset: int) -> Any:
    return svc.search(
        search_request=SimpleNamespace(query="bert", limit=limit, offset=offset),
        user_id="user-1",
        organization_id="org-1",
    )


def test_offset_is_forwarded_to_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, calls = _svc(monkeypatch, [_entity("e3")], total=None)

    _search(svc, limit=2, offset=2)

    assert calls["search"]["offset"] == 2
    assert calls["search"]["limit"] == 2


def test_full_page_reports_counted_total_and_has_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, calls = _svc(monkeypatch, [_entity("e1"), _entity("e2")], total=5)

    response = _search(svc, limit=2, offset=0)

    assert response.total_results == 5
    assert response.returned_results == 2
    assert response.has_more is True
    # The count uses the same scope/filters as the page query.
    assert calls["count"]["organization_id"] == "org-1"
    assert calls["count"]["require_source_document"] is True


def test_last_full_page_has_no_more(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _svc(monkeypatch, [_entity("e3"), _entity("e4")], total=4)

    response = _search(svc, limit=2, offset=2)

    assert response.total_results == 4
    assert response.has_more is False


def test_short_page_total_is_exact_without_a_count_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _ = _svc(monkeypatch, [_entity("e5")], total=None)

    response = _search(svc, limit=2, offset=4)

    assert response.total_results == 5
    assert response.has_more is False


def _session_returning(*rows: Any) -> tuple:
    session = MagicMock()
    session.run.return_value = list(rows)

    @contextmanager
    def _get_session(*_a: Any, **_k: Any) -> Iterator[MagicMock]:
        yield session

    return session, _get_session


def test_search_entities_skips_offset_rows() -> None:
    svc = KnowledgeGraphService()
    session, get_session = _session_returning()
    svc.get_session = get_session  # type: ignore[method-assign]

    svc.search_entities("bert", limit=10, offset=20, organization_id="org-1")

    query, params = session.run.call_args.args
    assert "SKIP $offset" in query
    assert query.index("SKIP $offset") < query.index("LIMIT $limit")
    assert params["offset"] == 20


def test_search_entities_list_mode_skips_offset_rows() -> None:
    svc = KnowledgeGraphService()
    session, get_session = _session_returning()
    svc.get_session = get_session  # type: ignore[method-assign]

    svc.search_entities("", limit=10, offset=20, organization_id="org-1")

    query, params = session.run.call_args.args
    assert "SKIP $offset" in query
    assert params["offset"] == 20


def test_count_search_entities_counts_with_the_same_filters() -> None:
    svc = KnowledgeGraphService()
    session, get_session = _session_returning({"total": 7})
    svc.get_session = get_session  # type: ignore[method-assign]

    total = svc.count_search_entities(
        "bert", organization_id="org-1", require_source_document=True
    )

    query, params = session.run.call_args.args
    assert total == 7
    assert "entity_fulltext_idx" in query
    assert "count(e)" in query
    assert "e.organization_id = $organization_id" in query
    assert "e.source_document_id IS NOT NULL" in query
    assert params["organization_id"] == "org-1"
    assert "SKIP" not in query and "LIMIT" not in query
