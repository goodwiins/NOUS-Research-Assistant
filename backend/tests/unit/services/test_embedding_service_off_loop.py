"""GOO-642 / IS-7: EmbeddingService must not block the event loop.

It loaded sentence-transformers synchronously before checking for Cohere (so
a Cohere deployment still paid the load, on the loop, from async callers),
and ran ``model.encode`` inside ``async def``.
"""

import asyncio
import time
from typing import Any, List

import numpy as np
import pytest

from src.core.config import settings
from src.models.vector import BatchEmbeddingRequest, EmbeddingRequest
from src.services.embedding import cohere_embed_service as cohere_module
from src.services.embedding import embedding_service as mod


class _Cohere:
    def __init__(self, enabled: bool) -> None:
        self.is_enabled = enabled


class _SlowModel:
    """Stands in for a SentenceTransformer whose encode takes 0.5 s."""

    def get_sentence_embedding_dimension(self) -> int:
        return 4

    def encode(self, texts: Any, convert_to_tensor: bool = True) -> Any:
        time.sleep(0.5)
        rows = 1 if isinstance(texts, str) else len(texts)
        arr = np.ones((rows, 4)) if not isinstance(texts, str) else np.ones(4)

        class _T:
            def cpu(self) -> "_T":
                return self

            def numpy(self) -> np.ndarray:
                return arr

        return _T()


@pytest.fixture
def no_remote_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cohere_module, "cohere_embed_service", _Cohere(False))
    monkeypatch.setattr(
        mod.azure_openai_service, "is_embedding_available", lambda: False
    )


@pytest.mark.unit
def test_cohere_deployment_never_loads_the_local_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: List[int] = []
    monkeypatch.setattr(cohere_module, "cohere_embed_service", _Cohere(True))
    monkeypatch.setattr(settings, "COHERE_EMBED_DIMENSIONS", 1024)
    monkeypatch.setattr(mod.EmbeddingService, "_load_model", lambda self: loads.append(1))

    service = mod.EmbeddingService()
    info = service.get_model_info()

    assert loads == []
    assert service.embedding_provider == "cohere"
    assert info["dimension"] == 1024


@pytest.mark.unit
def test_local_load_keeps_the_provider_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cohere_module, "cohere_embed_service", _Cohere(True))
    monkeypatch.setattr(settings, "COHERE_EMBED_DIMENSIONS", 1024)
    monkeypatch.setattr(mod, "_get_sentence_transformer", lambda *_: _SlowModel())

    service = mod.EmbeddingService()
    service._ensure_initialized()
    service._ensure_local_model()

    assert service.embedding_dimension == 1024
    assert service._local_dimension == 4


async def _measure_loop_ticks(coro: Any) -> tuple[Any, int]:
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    await asyncio.sleep(0)
    result = await coro
    seen = ticks
    await ticker
    return result, seen


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_encode_runs_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, no_remote_providers: None
) -> None:
    monkeypatch.setattr(mod, "_get_sentence_transformer", lambda *_: _SlowModel())
    service = mod.EmbeddingService()

    response, ticks = await _measure_loop_ticks(
        service.generate_embedding(EmbeddingRequest(text="hello"))
    )

    assert response.embedding == [1.0, 1.0, 1.0, 1.0]
    assert response.dimension == 4
    assert ticks >= 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_encode_runs_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, no_remote_providers: None
) -> None:
    monkeypatch.setattr(mod, "_get_sentence_transformer", lambda *_: _SlowModel())
    service = mod.EmbeddingService()

    response, ticks = await _measure_loop_ticks(
        service.generate_batch_embeddings(
            BatchEmbeddingRequest(texts=["a", "b"])
        )
    )

    assert response.embeddings == [[1.0] * 4, [1.0] * 4]
    assert response.failed_count == 0
    assert ticks >= 10
