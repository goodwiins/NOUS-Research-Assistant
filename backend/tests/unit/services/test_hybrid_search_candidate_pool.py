"""GOO-460: the hybrid candidate pool covers the requested page.

Each source arm fetched a fixed 20 candidates, so a caller asking for 50
(the research engine does) got at most 20 results, and any page past the
20th candidate was empty. The arms now fetch ``max(20, offset + limit)``,
capped at the ``SearchQuery.limit`` maximum the source request accepts.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.models.search_schemas import SearchQuery, SearchType
from src.services.knowledge_graph import knowledge_graph_service as kg_module
from src.services.search import hybrid_search_service as hybrid_module

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("offset", "limit", "expected"),
    [
        (0, 10, 20),  # small pages keep the old floor
        (0, 50, 50),  # research engine asks for 50
        (20, 10, 30),  # second page past the old cap
        (90, 50, 100),  # clamped to the SearchQuery.limit maximum
    ],
)
def test_source_arms_fetch_enough_candidates_for_the_page(
    monkeypatch: pytest.MonkeyPatch, offset: int, limit: int, expected: int
) -> None:
    seen: dict[str, int] = {}

    def _fulltext(search_request: SearchQuery, **_kwargs: Any) -> SimpleNamespace:
        seen["fulltext"] = search_request.limit
        return SimpleNamespace(results=[], total_results=0)

    def _entities(*, limit: int, **_kwargs: Any) -> list[Any]:
        seen["knowledge_graph"] = limit
        return []

    monkeypatch.setattr(hybrid_module.fulltext_search_service, "search", _fulltext)
    monkeypatch.setattr(kg_module.knowledge_graph_service, "search_entities", _entities)
    svc = hybrid_module.HybridSearchService(db=None)
    request = SearchQuery(
        query="q", search_type=SearchType.HYBRID, limit=limit, offset=offset
    )

    ft = svc._execute_fulltext_search(request, user_id="u", organization_id="org-1")
    kg = svc._execute_knowledge_graph_search(
        request, user_id="u", organization_id="org-1"
    )

    assert ft.success and kg.success
    assert seen == {"fulltext": expected, "knowledge_graph": expected}
