"""GOO-643 / IS2A-2: Kaggle bulk ingestion must not block the API event loop.

The kagglehub download, the multi-GB JSONL scan and ``json.loads`` per line
all ran inside ``async def``.
"""

import json
import sys
import threading
import types
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.services.ingestion import kaggle_llm_bulk_ingestion as mod

pytestmark = pytest.mark.unit


def _write_snapshot(directory: Path, categories: List[str]) -> None:
    lines = [
        json.dumps({"id": f"p{i}", "categories": cat})
        for i, cat in enumerate(categories)
    ]
    (directory / "arxiv-metadata-oai-snapshot.json").write_text("\n".join(lines))


def test_iter_papers_filters_by_category(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, ["cs.AI", "math.CO", "cs.LG cs.AI", "q-bio"])
    papers = mod._iter_papers(tmp_path / "arxiv-metadata-oai-snapshot.json", ["cs.AI"])

    assert [p["id"] for p in mod._take(papers, 10)] == ["p0", "p2"]


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def run(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Driver:
    def session(self) -> _Session:
        return _Session()

    async def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("max_papers", "start_batch", "expected_batches"),
    [
        (100, 0, [["p0", "p1"], ["p2", "p3"], ["p4"]]),  # full batches + remainder
        (100, 1, [["p2", "p3"], ["p4"]]),  # resume skips batch 1
        (2, 0, [["p0", "p1"]]),  # stops at max_papers
    ],
)
async def test_run_ingestion_reads_the_dataset_off_the_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_papers: int,
    start_batch: int,
    expected_batches: List[List[str]],
) -> None:
    _write_snapshot(tmp_path, ["cs.AI"] * 5)
    loop_thread = threading.current_thread()
    download_threads: List[threading.Thread] = []

    def _download(_name: str) -> str:
        download_threads.append(threading.current_thread())
        return str(tmp_path)

    monkeypatch.setitem(
        sys.modules, "kagglehub", types.SimpleNamespace(dataset_download=_download)
    )

    read_threads: List[threading.Thread] = []
    real_iter = mod._iter_papers

    def _recording_iter(*args: Any) -> Any:
        for paper in real_iter(*args):
            read_threads.append(threading.current_thread())
            yield paper

    monkeypatch.setattr(mod, "_iter_papers", _recording_iter)

    service = mod.KaggleLLMBulkIngestionService(batch_size=2, max_papers=max_papers)
    service.state_file = tmp_path / "state.json"
    service.state_file.write_text(json.dumps({"last_batch": start_batch}))
    service._neo4j_driver = _Driver()

    batches: List[List[str]] = []

    async def _process(
        _session: Any, papers: List[Dict[str, Any]], _n: int
    ) -> Dict[str, int]:
        batches.append([p["id"] for p in papers])
        return {"ingested": len(papers), "failed": 0, "skipped": 0}

    monkeypatch.setattr(service, "_process_batch_with_llm", _process)

    await service.run_ingestion(resume=True)

    assert batches == expected_batches
    assert download_threads and download_threads[0] is not loop_thread
    assert read_threads and all(t is not loop_thread for t in read_threads)


def test_closing_the_iterator_while_a_batch_is_running_does_not_raise() -> None:
    """On cancellation the worker thread can still be inside the generator;
    ``close()`` then raises ValueError, which would mask the CancelledError."""
    entered = threading.Event()
    release = threading.Event()

    def _papers() -> Any:
        entered.set()
        release.wait(timeout=5)
        yield {"id": "p0"}

    papers = _papers()
    worker = threading.Thread(target=lambda: mod._take(papers, 1))
    worker.start()
    assert entered.wait(timeout=5)
    try:
        mod._close_papers_iter(papers)  # must not raise
    finally:
        release.set()
        worker.join(timeout=5)

    mod._close_papers_iter(papers)  # a normal close still works
