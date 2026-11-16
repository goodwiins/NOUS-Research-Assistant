"""GOO-460: the hybrid candidate pool covers every page, identically.

Each source arm used to fetch a fixed 20 candidates, so a caller asking for 50
(the research engine does) got at most 20 results, and any page past the 20th
candidate was empty. #2141 sized the pool to ``offset + limit``, which made the
pool, and therefore the fused ranking, depend on the page being asked for:
fusion re-sorts with boosts (recency, multi-source diversity) that ignore the
source rank, so a deeper page could pull a candidate above ones an earlier page
had already shown. Results then repeated or went missing across pages, and
``has_more`` read false as soon as ``offset + limit`` reached the pool.

The arms now fetch a window sized by the page size alone (five pages, kept
between 20 and the ``SearchQuery.limit`` maximum of 100), never by the offset,
so every page of one size slices one ranking.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from src.models.document import DocumentType
from src.models.search_schemas import SearchQuery, SearchResult, SearchType
from src.services.knowledge_graph import knowledge_graph_service as kg_module
from src.services.search import hybrid_search_service as hybrid_module

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("limit", "window"),
    [
        (5, 25),  # agent RAG lookup: close to the old 20 rows
        (2, 20),  # floor
        (10, 50),  # five pages of 10
        (20, 100),
        (50, 100),  # research engine; capped at the source maximum
        (100, 100),
    ],
)
@pytest.mark.parametrize("offset", [0, 10, 20, 90])
def test_source_window_depends_on_page_size_never_offset(
    monkeypatch: pytest.MonkeyPatch, offset: int, limit: int, window: int
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
    assert seen == {"fulltext": window, "knowledge_graph": window}


_OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)
_RECENT_RANK = 24  # 25th by full-text rank; the recency boost lifts it to 1st


def _doc(rank: int) -> SearchResult:
    created = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
        if rank == _RECENT_RANK
        else _OLD
    )
    return SearchResult(
        document_id=f"doc-{rank:03d}",
        title=f"Document {rank}",
        document_type=DocumentType.PDF,
        content_preview="preview",
        relevance_score=9.9 - 0.1 * rank,  # ts_rank_cd * 10, strictly descending
        file_size_bytes=1,
        created_at=created,
        updated_at=created,
        processing_status="completed",
        is_public=False,
        uploaded_by_user_id="u",
        organization_id="org-1",
    )


@pytest.fixture
def ranked_corpus(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """45 full-text matches; the arm honours the requested limit like SQL does."""
    corpus = [_doc(rank) for rank in range(45)]

    def _fulltext(search_request: SearchQuery, **_kwargs: Any) -> SimpleNamespace:
        page = corpus[
            search_request.offset : search_request.offset + search_request.limit
        ]
        return SimpleNamespace(
            results=[d.model_copy(deep=True) for d in page],
            total_results=len(corpus),
        )

    monkeypatch.setattr(hybrid_module.fulltext_search_service, "search", _fulltext)
    monkeypatch.setattr(
        type(hybrid_module.cohere_rerank_service),
        "is_enabled",
        property(lambda _self: False),
    )
    return [d.document_id for d in corpus]


def _page(svc: Any, offset: int, limit: int) -> Any:
    # "q" has no entity indicator, so only the full-text arm runs.
    request = SearchQuery(
        query="q", search_type=SearchType.HYBRID, limit=limit, offset=offset
    )
    return svc.search(request, user_id="u", organization_id="org-1", db=None)


def test_pages_partition_one_ranking(ranked_corpus: list[str]) -> None:
    svc = hybrid_module.HybridSearchService(db=None)

    seen: list[str] = []
    pages = []
    for offset in range(0, 50, 10):
        page = _page(svc, offset, 10)
        pages.append(page)
        seen.extend(r.document_id for r in page.results)

    assert len(seen) == len(set(seen)), "a document appeared on two pages"
    assert sorted(seen) == sorted(ranked_corpus), "a document appeared on no page"
    # The boosted document leads page 1, not some deeper page.
    assert pages[0].results[0].document_id == f"doc-{_RECENT_RANK:03d}"
    assert [p.has_more for p in pages] == [True, True, True, True, False]
    assert {p.total_results for p in pages} == {45}


def test_has_more_is_true_when_the_first_page_fills_the_old_pool(
    ranked_corpus: list[str],
) -> None:
    svc = hybrid_module.HybridSearchService(db=None)

    first = _page(svc, 0, 20)
    second = _page(svc, 20, 20)

    assert first.has_more is True
    assert len(second.results) == 20
    assert not {r.document_id for r in first.results} & {
        r.document_id for r in second.results
    }


def test_has_more_ends_at_the_window_not_the_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With more matches than the window, paging stops at the window's end."""
    corpus = [_doc(rank) for rank in range(70)]

    def _fulltext(search_request: SearchQuery, **_kwargs: Any) -> SimpleNamespace:
        page = corpus[
            search_request.offset : search_request.offset + search_request.limit
        ]
        return SimpleNamespace(
            results=[d.model_copy(deep=True) for d in page],
            total_results=len(corpus),
        )

    monkeypatch.setattr(hybrid_module.fulltext_search_service, "search", _fulltext)
    monkeypatch.setattr(
        type(hybrid_module.cohere_rerank_service),
        "is_enabled",
        property(lambda _self: False),
    )
    svc = hybrid_module.HybridSearchService(db=None)

    last = _page(svc, 40, 10)  # window for limit 10 is 50 candidates
    past = _page(svc, 50, 10)

    assert len(last.results) == 10
    assert last.has_more is False
    assert last.total_results == 50
    assert past.results == [] and past.has_more is False
