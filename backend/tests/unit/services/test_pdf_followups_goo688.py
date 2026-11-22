"""GOO-688: follow-ups to the GOO-642 PDF work.

1. PyMuPDF is not thread-safe, so every threaded caller holds one
   process-wide lock (``pdf_lock.PYMUPDF_LOCK``).
3. ``extract_pdf_content`` has a timeout, so a stuck parse frees its slot.
4. The abandoned-slot branch in ``_run_bounded`` (worker starts after the
   caller gave up) neither runs the work nor releases the slot twice.
5. A pypdf ``NullObject`` metadata value is an absent field, not "NullObject".

Item 2 (a failed extraction marks the graph stage failed) is covered in
``tests/unit/tasks/test_replay_idempotency.py``.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, List
from uuid import uuid4

import fitz
import pytest

from src.services.processing import pdf_lock
from src.services.processing import table_extraction_service as tables

pytestmark = pytest.mark.unit

_REAL_FITZ_OPEN = fitz.open  # fixtures build PDFs outside the lock check


def _pdf_bytes(text: str = "hello") -> bytes:
    doc = _REAL_FITZ_OPEN()
    doc.new_page().insert_text((72, 72), text)
    data: bytes = doc.tobytes()
    doc.close()
    return data


class _RecordingLock:
    """Stands in for PYMUPDF_LOCK and records whether it is held."""

    def __init__(self) -> None:
        self.depth = 0
        self.entered = 0

    def __enter__(self) -> "_RecordingLock":
        self.depth += 1
        self.entered += 1
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.depth -= 1


@pytest.fixture
def lock_probe(monkeypatch: pytest.MonkeyPatch) -> Iterator[_RecordingLock]:
    """Swap in a recording lock and make ``fitz.open`` assert it is held."""
    probe = _RecordingLock()
    monkeypatch.setattr(pdf_lock, "PYMUPDF_LOCK", probe)
    def _guarded_open(*args: Any, **kwargs: Any) -> Any:
        assert probe.depth > 0, "fitz.open called without PYMUPDF_LOCK"
        return _REAL_FITZ_OPEN(*args, **kwargs)

    monkeypatch.setattr(fitz, "open", _guarded_open)
    yield probe


def test_pymupdf_lock_is_reentrant() -> None:
    with pdf_lock.PYMUPDF_LOCK:
        with pdf_lock.PYMUPDF_LOCK:
            pass


def test_table_extraction_holds_the_lock(
    tmp_path: Path, lock_probe: _RecordingLock, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "a.pdf"
    path.write_bytes(_pdf_bytes("region text"))

    @contextmanager
    def _local(document: Any) -> Iterator[str]:
        yield document.file_path

    monkeypatch.setattr(tables, "local_file_for_document", _local)
    doc = SimpleNamespace(storage_backend="supabase-test", file_path=str(path))

    service = tables.TableExtractionService()
    result = service._extract_region_sync(doc, 1, 0, 0, 600, 200)

    assert "region text" in result["content"]
    assert tables.TableExtractionService._page_range(str(path)) == "1-1"
    assert lock_probe.entered == 2


def test_figure_extraction_holds_the_lock(
    tmp_path: Path, lock_probe: _RecordingLock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.core.config import settings
    from src.models.document import DocumentType
    from src.services.processing import figure_extraction_service as figures

    path = tmp_path / "f.pdf"
    path.write_bytes(_pdf_bytes())

    @contextmanager
    def _local(document: Any) -> Iterator[str]:
        yield str(path)

    monkeypatch.setattr(settings, "FIGURE_EXTRACTION_ENABLED", True)
    monkeypatch.setattr(figures, "local_file_for_document", _local)
    monkeypatch.setattr(figures, "_collect_candidates", lambda _doc: [])

    class _Db:
        def query(self, *_a: Any) -> Any:
            raise AssertionError("no candidates: nothing to persist")

    document = SimpleNamespace(
        document_type=DocumentType.PDF, id=uuid4(), organization_id=uuid4()
    )
    try:
        figures.extract_figures_for_document(_Db(), document)  # type: ignore[arg-type]
    except AssertionError as exc:
        if "PYMUPDF_LOCK" in str(exc):
            raise
    assert lock_probe.entered >= 1


async def test_citation_pdf_extraction_holds_the_lock(
    tmp_path: Path, lock_probe: _RecordingLock
) -> None:
    from src.services.research.citation_extraction_service import (
        CitationExtractionService,
    )

    path = tmp_path / "c.pdf"
    path.write_bytes(_pdf_bytes("doi:10.1234/abcd.5678"))
    document = SimpleNamespace(id=uuid4(), file_path=str(path), title="T")

    class _Result:
        def scalar_one_or_none(self) -> Any:
            return document

    class _Db:
        async def execute(self, _q: Any) -> _Result:
            return _Result()

    service = CitationExtractionService(_Db())  # type: ignore[arg-type]
    await service.extract_from_pdf(document.id)

    assert lock_probe.entered == 1


async def test_stuck_pdf_parse_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.services.arxiv import arxiv_service as arxiv

    async def _stuck(_content: bytes) -> dict:
        await asyncio.sleep(30)
        return {}

    monkeypatch.setattr(arxiv, "_parse_pdf_off_loop", _stuck)
    monkeypatch.setattr(arxiv, "_PDF_PARSE_TIMEOUT_SECONDS", 0.05)
    service = arxiv.ArXivIngestionService.__new__(arxiv.ArXivIngestionService)

    with pytest.raises(arxiv.IngestionError):
        await asyncio.wait_for(service.extract_pdf_content(b"%PDF"), 2)


class _UncancellableExecutor(ThreadPoolExecutor):
    """Hands back an already-running future and calls ``fn`` once ``gate``
    opens, so the caller can give up before the worker body starts: the
    race the abandoned branch exists for."""

    def __init__(self, gate: threading.Event) -> None:
        super().__init__(max_workers=1)
        self.gate = gate
        self.outcomes: List[str] = []

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        future: Future = Future()
        future.set_running_or_notify_cancel()  # running: cancel() now fails

        def _run() -> None:
            self.gate.wait(timeout=5)
            try:
                future.set_result(fn(*args, **kwargs))
                self.outcomes.append("ran")
            except BaseException as exc:  # noqa: BLE001 - recorded for the test
                self.outcomes.append(type(exc).__name__)
                future.set_exception(exc)

        threading.Thread(target=_run, daemon=True).start()
        return future


async def test_abandoned_slot_branch_skips_work_and_releases_once() -> None:
    gate = threading.Event()
    executor = _UncancellableExecutor(gate)
    loop = asyncio.get_running_loop()
    loop.set_default_executor(executor)
    work: List[bool] = []

    task = asyncio.create_task(tables._run_bounded(lambda: work.append(True)))
    await asyncio.sleep(0.05)  # submitted; the worker body is held at the gate
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The caller released the slot it will never hand over.
    assert tables._extraction_slots.acquire(blocking=False)
    tables._extraction_slots.release()

    gate.set()  # now the worker body runs and finds the call abandoned
    for _ in range(100):
        if executor.outcomes:
            break
        await asyncio.sleep(0.01)

    assert executor.outcomes == ["CancelledError"]
    assert work == []
    # Released exactly once: a second release would overflow the
    # BoundedSemaphore (ValueError), and the slot is free again now.
    assert tables._extraction_slots.acquire(blocking=False)
    tables._extraction_slots.release()
    with pytest.raises(ValueError):
        tables._extraction_slots.release()


def test_null_metadata_is_an_absent_field() -> None:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, NullObject, TextStringObject

    from src.services.arxiv import arxiv_service as arxiv

    writer = PdfWriter(clone_from=PdfReader(BytesIO(_pdf_bytes())))
    writer.add_metadata({"/Author": "A. Author"})
    info: Any = writer._info
    info.get_object()[NameObject("/Title")] = NullObject()
    info.get_object()[NameObject("/Subject")] = writer._add_object(NullObject())
    info.get_object()[NameObject("/Creator")] = TextStringObject("maker")
    out = BytesIO()
    writer.write(out)

    metadata = arxiv._extract_pdf_content_sync(out.getvalue())["metadata"]

    assert metadata["title"] == ""
    assert metadata["subject"] == ""
    assert metadata["author"] == "A. Author"
    assert metadata["creator"] == "maker"
