"""GOO-589: POST /search with a graph search_type answers 503 on a Neo4j outage.

The KG routes map Neo4j unavailability to 503 (#2130), but the generic
``search_documents`` handler let it fall into its blanket ``except Exception``
and answered 500, so clients could not tell a dependency outage from a bug.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from neo4j.exceptions import ServiceUnavailable

from src.api.search import search as search_api
from src.core.circuit_breaker import ServiceUnavailableError as CircuitBreakerError
from src.models.search_schemas import SearchQuery, SearchSortOrder, SearchType
from src.services.knowledge_graph.knowledge_graph_service import knowledge_graph_service

pytestmark = pytest.mark.unit


def _user() -> Any:
    # Stands in for the ``User`` dependency; only ``id`` and
    # ``organization_id`` are read on this path.
    return SimpleNamespace(id="user-1", organization_id="org-1")


async def _run(search_type: SearchType) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        await search_api.search_documents(
            search_request=SearchQuery(
                query="bert",
                search_type=search_type,
                sort_order=SearchSortOrder.RELEVANCE,
                filters=None,
            ),
            background_tasks=BackgroundTasks(),
            current_user=_user(),
            db=MagicMock(),
        )
    return exc.value


@pytest.mark.parametrize("search_type", [SearchType.KNOWLEDGE_GRAPH, SearchType.GRAPH])
@pytest.mark.parametrize(
    "error",
    [
        ServiceUnavailable("Unable to retrieve routing information"),
        CircuitBreakerError("Circuit breaker is open for neo4j"),
    ],
)
async def test_graph_search_returns_503_when_neo4j_unavailable(
    monkeypatch: pytest.MonkeyPatch, search_type: SearchType, error: Exception
) -> None:
    def _down(**_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(knowledge_graph_service, "search", _down)

    exc = await _run(search_type)

    assert exc.status_code == 503
    assert exc.detail == "Knowledge graph temporarily unavailable"
    assert exc.headers == {"Retry-After": "5"}


async def test_graph_search_other_errors_stay_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _bug(**_kwargs: Any) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr(knowledge_graph_service, "search", _bug)

    exc = await _run(SearchType.KNOWLEDGE_GRAPH)

    assert exc.status_code == 500
    assert exc.detail == "Internal server error"
