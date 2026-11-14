"""GOO-501: the search quality benchmark only runs search types it can execute.

The default list included ``SearchType.VECTOR``, which the search API rejects
(the Qdrant arm was removed) and the benchmark loop silently skipped. An
explicit request for any type the loop cannot run was skipped the same way,
so a caller got a result with that type missing and no error.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.api.search import search_quality as quality_api
from src.models.search_schemas import SearchType
from src.models.user import UserRole
from src.services.search import search_quality_service as quality_module

pytestmark = pytest.mark.unit


def test_default_benchmark_runs_only_supported_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    def _search(name: str) -> Any:
        def _run(**_kwargs: Any) -> SimpleNamespace:
            called.append(name)
            return SimpleNamespace(results=[])

        return _run

    monkeypatch.setattr(
        quality_module.hybrid_search_service, "search", _search("hybrid")
    )
    monkeypatch.setattr(
        quality_module.fulltext_search_service, "search", _search("fulltext")
    )
    service = quality_module.SearchQualityService()
    monkeypatch.setattr(service, "evaluate_search", lambda **_kwargs: "evaluation")

    results = service.run_quality_benchmark(["q"], organization_id="org-1")

    assert SearchType.VECTOR not in quality_module.BENCHMARK_SEARCH_TYPES
    assert sorted(called) == ["fulltext", "hybrid"]
    assert set(results["q"]) == {"fulltext", "hybrid"}


@pytest.mark.parametrize(
    "unsupported",
    [SearchType.VECTOR, SearchType.SEMANTIC, SearchType.KNOWLEDGE_GRAPH],
)
async def test_benchmark_api_rejects_unsupported_types(
    monkeypatch: pytest.MonkeyPatch, unsupported: SearchType
) -> None:
    def _forbid(**_kwargs: Any) -> Any:
        raise AssertionError("benchmark must not run for an unsupported type")

    monkeypatch.setattr(
        quality_api.search_quality_service, "run_quality_benchmark", _forbid
    )
    user = SimpleNamespace(id="user-1", role=UserRole.ADMIN, organization_id="org-1")

    with pytest.raises(HTTPException) as exc:
        await quality_api.run_quality_benchmark(
            request=quality_api.BenchmarkRequest(
                test_queries=["q"], search_types=[SearchType.HYBRID, unsupported]
            ),
            background_tasks=BackgroundTasks(),
            current_user=user,
        )

    assert exc.value.status_code == 400
    assert unsupported.value in str(exc.value.detail)
