"""GOO-701: a parse cancelled while still queued must give its slot back.

In the Celery prefork thread fallback, each PDF parse holds one of
``_MAX_THREAD_PARSES`` process-wide slots, released by the parse thread when
the parse ends (GOO-695). If the await was cancelled while the job still sat
in a busy executor's queue, the job never ran, so nothing released its slot:
after two such cancels the worker refused every parse until it restarted.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List

import pytest

from src.services.arxiv import arxiv_service as arxiv

pytestmark = pytest.mark.unit


class _CountingSlots(threading.BoundedSemaphore):
    """Counts releases, and records a release past the bound instead of only
    raising it inside a worker thread or callback where nobody sees it."""

    def __init__(self, value: int) -> None:
        super().__init__(value)
        self.releases = 0
        self.over_released = 0

    def release(self, n: int = 1) -> None:
        self.releases += n
        try:
            super().release(n)
        except ValueError:
            self.over_released += n
            raise


@pytest.fixture
async def busy_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[threading.Event]:
    """A one-thread executor whose only worker is blocked until released."""
    monkeypatch.setattr(
        arxiv.multiprocessing, "current_process", lambda: SimpleNamespace(daemon=True)
    )
    monkeypatch.setattr(arxiv, "_thread_parse_slots", _CountingSlots(2))
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(arxiv, "_pdf_parse_executor", executor, raising=False)
    asyncio.get_running_loop().set_default_executor(executor)

    unblock = threading.Event()
    executor.submit(unblock.wait, 10)
    yield unblock
    unblock.set()
    executor.shutdown(wait=True)


async def test_cancelled_queued_parses_release_their_slots(
    busy_executor: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: List[bytes] = []

    def _parse(pdf: bytes) -> Dict[str, Any]:
        ran.append(pdf)
        return {"full_text": "", "page_texts": [], "num_pages": 0, "metadata": {}}

    monkeypatch.setattr(arxiv, "_extract_pdf_content_sync", _parse)

    for _ in range(3):  # more cancels than there are slots
        task = asyncio.create_task(arxiv._parse_pdf_off_loop(b"%PDF"))
        await asyncio.sleep(0.02)  # acquired its slot, queued behind the blocker
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    busy_executor.set()
    for _ in range(50):
        if arxiv._thread_parse_slots._value == 2:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.02)

    assert arxiv._thread_parse_slots._value == 2, "a cancelled queued parse kept its slot"  # type: ignore[attr-defined]
    assert ran == [], "a cancelled parse ran anyway"
    assert arxiv._thread_parse_slots.over_released == 0  # type: ignore[attr-defined]
    # And the worker still parses afterwards.
    assert (await arxiv._parse_pdf_off_loop(b"%PDF"))["num_pages"] == 0
    assert arxiv._thread_parse_slots._value == 2  # type: ignore[attr-defined]


async def test_a_parse_that_ran_releases_its_slot_exactly_once(
    busy_executor: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    busy_executor.set()
    started, finish = threading.Event(), threading.Event()

    def _parse(pdf: bytes) -> Dict[str, Any]:
        started.set()
        finish.wait(5)
        return {"full_text": "", "page_texts": [], "num_pages": 1, "metadata": {}}

    monkeypatch.setattr(arxiv, "_extract_pdf_content_sync", _parse)

    # Cancelled while running: the thread finishes and releases; no second
    # release from the cancel path (a BoundedSemaphore would raise ValueError).
    task = asyncio.create_task(arxiv._parse_pdf_off_loop(b"%PDF"))
    for _ in range(250):  # not to_thread: the only worker is busy parsing
        if started.is_set():
            break
        await asyncio.sleep(0.02)
    assert started.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    finish.set()
    for _ in range(50):
        if arxiv._thread_parse_slots._value == 2:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.02)
    assert arxiv._thread_parse_slots._value == 2  # type: ignore[attr-defined]
    slots = arxiv._thread_parse_slots
    assert slots.releases == 1 and slots.over_released == 0  # type: ignore[attr-defined]
