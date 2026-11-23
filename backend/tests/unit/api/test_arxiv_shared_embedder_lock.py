"""GOO-693: the shared embedding service is built once, even under a race.

``_get_shared_embedding_service`` runs in worker threads. Without a lock,
concurrent ingests all saw ``None`` and each loaded the model.
"""

import threading
import time
from typing import Any, List

import pytest

from src.api.arxiv import arxiv_local as mod

pytestmark = pytest.mark.unit


def test_concurrent_first_calls_build_one_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds: List[Any] = []

    class _SlowEmbeddingService:
        def __init__(self, lazy: bool = True) -> None:
            time.sleep(0.05)  # a model load: wide enough for every racer
            builds.append(self)

    monkeypatch.setattr(mod, "EmbeddingService", _SlowEmbeddingService)
    monkeypatch.setattr(mod, "_shared_embedding_service", None)

    start = threading.Barrier(8)
    got: List[Any] = []

    def _racer() -> None:
        start.wait()
        got.append(mod._get_shared_embedding_service())

    threads = [threading.Thread(target=_racer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(builds) == 1, f"model built {len(builds)} times"
    assert len(got) == 8 and all(s is builds[0] for s in got)
