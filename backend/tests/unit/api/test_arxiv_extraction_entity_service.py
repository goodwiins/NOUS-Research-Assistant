"""GOO-642 / API2A-2: /arxiv/extract-features keeps spaCy off the event loop.

The route built a new EntityExtractionService (a spaCy model load) per
request and ran NLP for each paper inside ``async def``.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import patch

import pytest

from src.api.arxiv import arxiv_extraction as mod

pytestmark = pytest.mark.unit


class _SlowEntityService:
    builds: List[int] = []

    def __init__(self) -> None:
        _SlowEntityService.builds.append(1)

    def extract_entities_from_text(self, document: Any, text: str) -> list:
        time.sleep(0.3)
        return [{"id": "e1", "name": "BERT", "entity_type": "model"}]


class _Arxiv:
    async def __aenter__(self) -> "_Arxiv":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def search_papers(self, query: str, max_results: int) -> list:
        return [{"title": "t", "abstract": "BERT is a model."}]


@pytest.fixture(autouse=True)
def _fresh_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_entity_service", None)
    monkeypatch.setattr(mod, "EntityExtractionService", _SlowEntityService)
    _SlowEntityService.builds = []


def _request() -> mod.ExtractionRequest:
    return mod.ExtractionRequest(
        paper_ids=["2401.00001", "2401.00002"],
        extract_topics=False,
        extract_keyphrases=False,
        extract_summaries=False,
        extract_citations=False,
        update_knowledge_graph=False,
    )


async def test_entity_service_is_built_once_and_nlp_runs_off_loop() -> None:
    user: Any = SimpleNamespace(organization_id="org-1", organization=None)
    background: Any = None
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(40):
            await asyncio.sleep(0.01)
            ticks += 1

    with patch.object(mod, "ArXivIngestionService", _Arxiv):
        ticker = asyncio.create_task(_ticker())
        await asyncio.sleep(0)
        first = await mod._extract_paper_features(_request(), background, user)
        seen = ticks
        await ticker
        await mod._extract_paper_features(_request(), background, user)

    assert _SlowEntityService.builds == [1]
    entities = first.results[0]["features"]["entities"]
    assert entities["count"] == 1
    # Two papers x 0.3 s of NLP: a blocking call would freeze the ticker.
    assert seen >= 20
