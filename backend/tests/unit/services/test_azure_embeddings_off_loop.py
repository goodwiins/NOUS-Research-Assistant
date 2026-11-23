"""GOO-643 / PL-6: Azure embeddings must not block the event loop.

``AzureOpenAIService.get_embeddings`` made one synchronous ``embeddings.create``
call per text inside ``async def``.
"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, List

import pytest

from src.services.infrastructure.azure_openai_service import AzureOpenAIService

pytestmark = pytest.mark.unit


class _Embeddings:
    def __init__(self) -> None:
        self.calls: List[List[str]] = []

    def create(self, input: List[str], model: str) -> Any:
        self.calls.append(list(input))
        time.sleep(0.2)
        # Out of order on purpose: the service must reorder by ``index``.
        data = [
            SimpleNamespace(index=i, embedding=[float(len(text))])
            for i, text in enumerate(input)
        ]
        return SimpleNamespace(data=list(reversed(data)))


def _service(embeddings: _Embeddings) -> AzureOpenAIService:
    service = AzureOpenAIService.__new__(AzureOpenAIService)
    service.embedding_client = SimpleNamespace(embeddings=embeddings)
    service.get_embedding_deployment = lambda: "embed-deploy"  # type: ignore[method-assign]
    return service


async def test_embeddings_are_batched_ordered_and_off_loop() -> None:
    embeddings = _Embeddings()
    service = _service(embeddings)
    texts = ["a" * (i + 1) for i in range(AzureOpenAIService._EMBEDDING_BATCH_SIZE + 2)]

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    await asyncio.sleep(0)
    result = await service.get_embeddings(texts)
    seen = ticks
    await ticker

    assert result == [[float(len(t))] for t in texts]
    assert [len(c) for c in embeddings.calls] == [
        AzureOpenAIService._EMBEDDING_BATCH_SIZE,
        2,
    ]
    assert seen >= 10
