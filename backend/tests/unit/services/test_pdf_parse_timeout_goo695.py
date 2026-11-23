"""GOO-695: the PDF parse timeout in a Celery prefork worker.

A daemonic process cannot start the per-call worker process, so the parse
falls back to a thread. The timeout then only abandons the await: the thread
keeps parsing. Each timed-out parse used to leave one more such thread, with
no bound. And the timeout surfaced as "PDF extraction failed: " with nothing
after it, since ``str(asyncio.TimeoutError())`` is empty.
"""

import asyncio
import threading
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List

import pytest

from src.services.arxiv import arxiv_service as arxiv

IngestionError = arxiv.IngestionError

pytestmark = pytest.mark.unit


@pytest.fixture
def prefork(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        arxiv.multiprocessing, "current_process", lambda: SimpleNamespace(daemon=True)
    )
    # A fresh slot pool per test, sized 2.
    monkeypatch.setattr(
        arxiv, "_thread_parse_slots", threading.BoundedSemaphore(2), raising=False
    )


@pytest.fixture
def hung_parse(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    release = threading.Event()
    started: List[bool] = []

    def _hung(_pdf: bytes) -> Dict[str, Any]:
        started.append(True)
        release.wait(timeout=10)
        return {"full_text": "", "page_texts": [], "num_pages": 0, "metadata": {}}

    monkeypatch.setattr(arxiv, "_extract_pdf_content_sync", _hung)
    monkeypatch.setattr(arxiv, "_PDF_PARSE_TIMEOUT_SECONDS", 0.05)
    yield SimpleNamespace(release=release, started=started)
    release.set()


async def test_timeouts_cannot_pile_up_parse_threads(
    prefork: None, hung_parse: SimpleNamespace
) -> None:
    service = arxiv.ArXivIngestionService.__new__(arxiv.ArXivIngestionService)

    for _ in range(2):
        with pytest.raises(IngestionError, match="timed out"):
            await service.extract_pdf_content(b"%PDF")

    # Both slots are still held by threads that are really still parsing, so
    # a third parse is refused at once instead of starting another thread.
    with pytest.raises(IngestionError, match="still running"):
        await service.extract_pdf_content(b"%PDF")
    assert len(hung_parse.started) == 2

    # Once the hung parses end, their slots come back.
    hung_parse.release.set()
    for _ in range(50):
        if arxiv._thread_parse_slots._value == 2:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.02)
    assert arxiv._thread_parse_slots._value == 2  # type: ignore[attr-defined]


async def test_timeout_error_message_is_not_empty(
    prefork: None, hung_parse: SimpleNamespace
) -> None:
    service = arxiv.ArXivIngestionService.__new__(arxiv.ArXivIngestionService)
    with pytest.raises(IngestionError) as info:
        await service.extract_pdf_content(b"%PDF")
    message = str(info.value)
    assert message.rstrip().endswith(":") is False
    assert "timed out after" in message


async def test_a_parse_that_finishes_frees_its_slot(
    prefork: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        arxiv,
        "_extract_pdf_content_sync",
        lambda _pdf: {
            "full_text": "x",
            "page_texts": [],
            "num_pages": 1,
            "metadata": {},
        },
    )
    service = arxiv.ArXivIngestionService.__new__(arxiv.ArXivIngestionService)
    for _ in range(5):
        assert (await service.extract_pdf_content(b"%PDF"))["full_text"] == "x"
    assert arxiv._thread_parse_slots._value == 2  # type: ignore[attr-defined]
