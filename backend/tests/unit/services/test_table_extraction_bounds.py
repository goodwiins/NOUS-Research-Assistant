"""GOO-642 (IS2B-2 / API2A-3): table extraction is off-loop and bounded.

The S3 download, PyMuPDF and Camelot/Ghostscript all ran synchronously
inside ``async def`` with no size, page or concurrency bound.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import patch
from uuid import uuid4

import fitz
import pytest
from fastapi import HTTPException

from src.api.documents import table_extraction as router_mod
from src.services.processing import table_extraction_service as mod

pytestmark = pytest.mark.unit


def _pdf(path: str, pages: int = 1) -> None:
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    doc.save(path)
    doc.close()


def _doc(path: str, size: int = 1024) -> SimpleNamespace:
    return SimpleNamespace(
        storage_backend="supabase-test",  # skips the UPLOAD_DIR path guard
        file_path=path,
        filename="x.pdf",
        id=uuid4(),
        file_size_bytes=size,
    )


@pytest.fixture(autouse=True)
def _local_file(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager

    @contextmanager
    def _yield_path(document: Any):
        yield document.file_path

    monkeypatch.setattr(mod, "local_file_for_document", _yield_path)


async def test_region_extraction_runs_off_the_loop(tmp_path, monkeypatch) -> None:
    path = str(tmp_path / "a.pdf")
    _pdf(path)
    service = mod.TableExtractionService()
    real = service._extract_region_sync

    def _slow(*args: Any) -> Any:
        time.sleep(0.5)
        return real(*args)

    monkeypatch.setattr(service, "_extract_region_sync", _slow)

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    await asyncio.sleep(0)
    result = await service.extract_region(_doc(path), 1, 0, 0, 600, 200)
    seen = ticks
    await ticker

    assert "page 1" in result["content"]
    assert seen >= 10


async def test_oversized_document_is_rejected_before_download(tmp_path) -> None:
    service = mod.TableExtractionService()
    big = _doc(str(tmp_path / "missing.pdf"), size=mod.MAX_EXTRACTION_PDF_BYTES + 1)

    with pytest.raises(mod.DocumentTooLargeError):
        await service.extract_region(big, 1, 0, 0, 10, 10)
    with patch.dict("sys.modules", {"camelot": SimpleNamespace()}):
        with pytest.raises(mod.DocumentTooLargeError):
            await service.extract_tables_from_pdf(big)


def test_camelot_page_range_is_capped(tmp_path) -> None:
    short, long_ = str(tmp_path / "s.pdf"), str(tmp_path / "l.pdf")
    _pdf(short, pages=3)
    _pdf(long_, pages=mod.MAX_TABLE_PAGES + 5)

    assert mod.TableExtractionService._page_range(short) == "1-3"
    assert (
        mod.TableExtractionService._page_range(long_) == f"1-{mod.MAX_TABLE_PAGES}"
    )


def test_tables_pass_the_capped_range_to_camelot(tmp_path) -> None:
    path = str(tmp_path / "l.pdf")
    _pdf(path, pages=mod.MAX_TABLE_PAGES + 5)
    calls: List[dict] = []

    def _read_pdf(_path: str, **kwargs: Any) -> list:
        calls.append(kwargs)
        return []

    with patch.dict("sys.modules", {"camelot": SimpleNamespace(read_pdf=_read_pdf)}):
        assert mod.TableExtractionService()._extract_tables_sync(_doc(path)) == []

    assert calls == [{"flavor": "lattice", "pages": f"1-{mod.MAX_TABLE_PAGES}"}]


async def test_full_slots_reject_instead_of_queueing(tmp_path, monkeypatch) -> None:
    path = str(tmp_path / "a.pdf")
    _pdf(path)
    service = mod.TableExtractionService()
    release = threading.Event()
    started = threading.Semaphore(0)

    def _blocked(*_args: Any) -> dict:
        started.release()
        release.wait(timeout=5)
        return {"format": "markdown", "content": "", "confidence": 0.0}

    monkeypatch.setattr(service, "_extract_region_sync", _blocked)
    running = [
        asyncio.create_task(service.extract_region(_doc(path), 1, 0, 0, 10, 10))
        for _ in range(mod.MAX_CONCURRENT_EXTRACTIONS)
    ]
    for _ in running:
        await asyncio.to_thread(started.acquire, True, 5)

    with pytest.raises(mod.ExtractionBusyError):
        await service.extract_region(_doc(path), 1, 0, 0, 10, 10)

    release.set()
    await asyncio.gather(*running)
    # Slots are released afterwards.
    monkeypatch.setattr(service, "_extract_region_sync", lambda *_: {"ok": True})
    assert await service.extract_region(_doc(path), 1, 0, 0, 10, 10) == {"ok": True}


class _Result:
    def __init__(self, document: Any) -> None:
        self._document = document

    def scalar_one_or_none(self) -> Any:
        return self._document


class _Db:
    def __init__(self, document: Any) -> None:
        self._document = document

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._document)


@pytest.mark.parametrize(
    ("error", "code"),
    [(mod.ExtractionBusyError(), 429), (mod.DocumentTooLargeError(), 413)],
)
async def test_routes_map_limit_errors(monkeypatch, error: Exception, code: int) -> None:
    document = SimpleNamespace(file_path="x.pdf")
    org = SimpleNamespace(id=uuid4())

    async def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(router_mod._service, "extract_tables_from_pdf", _raise)
    monkeypatch.setattr(router_mod._service, "extract_region", _raise)

    with pytest.raises(HTTPException) as tables_exc:
        await router_mod.extract_tables(uuid4(), None, org, _Db(document))
    body = router_mod.ExtractRegionRequest(page=1, x1=0, y1=0, x2=1, y2=1)
    with pytest.raises(HTTPException) as region_exc:
        await router_mod.extract_region(uuid4(), body, None, org, _Db(document))

    assert tables_exc.value.status_code == code
    assert region_exc.value.status_code == code
