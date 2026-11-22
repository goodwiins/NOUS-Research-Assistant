"""GOO-695: the remaining PyMuPDF callers hold ``PYMUPDF_LOCK`` too.

``ProcessingPipeline._extract_text_from_pdf`` and the two
``MultimodalProcessingService`` PDF steps still called fitz without the
lock. They run in Celery workers, where an abandoned table-extraction thread
can still be inside fitz when the next stage opens a PDF.

Every fitz call must hold the lock, but OCR (pytesseract, seconds per page)
must not: holding the lock across it would stall every other PDF path.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, List
from uuid import uuid4

import fitz
import pytesseract
import pytest

from src.services.processing import multimodal_processing_service as mm
from src.services.processing import pdf_lock

pytestmark = pytest.mark.unit

_REAL_FITZ_OPEN = fitz.open


class _Probe:
    def __init__(self) -> None:
        self.depth = 0
        self.fitz_calls = 0
        self.ocr_under_lock: List[bool] = []

    def __enter__(self) -> "_Probe":
        self.depth += 1
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.depth -= 1

    def check(self, what: str) -> None:
        self.fitz_calls += 1
        assert self.depth > 0, f"fitz {what} called without PYMUPDF_LOCK"


class _Guarded:
    """Wraps a fitz object: every method call and attribute read must hold
    the lock; returned fitz objects (pages, pixmaps) are wrapped too."""

    def __init__(self, obj: Any, probe: _Probe) -> None:
        object.__setattr__(self, "_obj", obj)
        object.__setattr__(self, "_probe", probe)

    def __getattr__(self, name: str) -> Any:
        probe: _Probe = object.__getattribute__(self, "_probe")
        obj = object.__getattribute__(self, "_obj")
        probe.check(name)
        attr = getattr(obj, name)
        if not callable(attr):
            return attr

        def _call(*args: Any, **kwargs: Any) -> Any:
            probe.check(f"{name}()")
            result = attr(*args, **kwargs)
            if type(result).__module__.startswith(("fitz", "pymupdf")):
                return _Guarded(result, probe)
            return result

        return _call

    def __len__(self) -> int:
        probe: _Probe = object.__getattribute__(self, "_probe")
        probe.check("len()")
        return len(object.__getattribute__(self, "_obj"))

    def __enter__(self) -> "_Guarded":
        object.__getattribute__(self, "_probe").check("__enter__")
        return self

    def __exit__(self, *exc: Any) -> None:
        object.__getattribute__(self, "_probe").check("__exit__")
        object.__getattribute__(self, "_obj").close()


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Probe]:
    p = _Probe()
    monkeypatch.setattr(pdf_lock, "PYMUPDF_LOCK", p)

    def _open(*args: Any, **kwargs: Any) -> Any:
        p.check("open")
        return _Guarded(_REAL_FITZ_OPEN(*args, **kwargs), p)

    monkeypatch.setattr(fitz, "open", _open)

    def _ocr(*_a: Any, **_k: Any) -> Any:
        p.ocr_under_lock.append(p.depth > 0)
        return "ocr text"

    monkeypatch.setattr(pytesseract, "image_to_string", _ocr)

    def _ocr_data(*_a: Any, **_k: Any) -> Any:
        p.ocr_under_lock.append(p.depth > 0)
        return {"conf": []}

    monkeypatch.setattr(pytesseract, "image_to_data", _ocr_data)
    yield p


def _pdf(path: Path, *, text: str | None) -> str:
    doc = _REAL_FITZ_OPEN()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    doc.new_page()  # a blank page: the OCR path
    doc.save(str(path))
    doc.close()
    return str(path)


def _processing_service() -> Any:
    from src.services.processing.processing_service import ProcessingPipeline

    service = ProcessingPipeline.__new__(ProcessingPipeline)
    return service


def test_processing_service_pdf_text_holds_the_lock(
    tmp_path: Path, probe: _Probe
) -> None:
    path = _pdf(tmp_path / "a.pdf", text="direct text")

    text = _processing_service()._extract_text_from_pdf(path)

    assert "direct text" in text and "ocr text" in text
    assert probe.fitz_calls > 0
    assert probe.ocr_under_lock == [False], "OCR ran while holding PYMUPDF_LOCK"


def test_processing_service_ocr_fallback_holds_the_lock(
    tmp_path: Path, probe: _Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _pdf(tmp_path / "b.pdf", text="x")
    calls = {"n": 0}
    real_open = fitz.open

    def _first_open_fails(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            probe.check("open")
            raise RuntimeError("damaged xref")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(fitz, "open", _first_open_fails)

    text = _processing_service()._extract_text_from_pdf(path)

    assert "ocr text" in text
    assert probe.ocr_under_lock and not any(probe.ocr_under_lock)


@contextmanager
def _local(path: str) -> Iterator[str]:
    yield path


async def test_multimodal_process_pdf_holds_the_lock(
    tmp_path: Path, probe: _Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _pdf(tmp_path / "c.pdf", text="multimodal text")
    monkeypatch.setattr(mm, "local_file_for_document", lambda _doc: _local(path))
    # pdfplumber is not installed, so OCR_AVAILABLE is False and these steps
    # return early; force them on to exercise the fitz calls.
    monkeypatch.setattr(mm, "OCR_AVAILABLE", True)
    service = mm.MultimodalProcessingService.__new__(mm.MultimodalProcessingService)
    document = SimpleNamespace(id=uuid4())

    result = await service.process_pdf(document, None)  # type: ignore[arg-type]

    assert "multimodal text" in result["text_content"]
    assert result["page_count"] == 2
    assert probe.fitz_calls > 0


async def test_multimodal_ocr_holds_the_lock_but_not_during_ocr(
    tmp_path: Path, probe: _Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _pdf(tmp_path / "d.pdf", text=None)
    monkeypatch.setattr(mm, "local_file_for_document", lambda _doc: _local(path))
    # pdfplumber is not installed, so OCR_AVAILABLE is False and these steps
    # return early; force them on to exercise the fitz calls.
    monkeypatch.setattr(mm, "OCR_AVAILABLE", True)
    service = mm.MultimodalProcessingService.__new__(mm.MultimodalProcessingService)
    document = SimpleNamespace(id=uuid4())

    result = await service.extract_text_with_ocr(document, None)  # type: ignore[arg-type]

    assert result["ocr_text"].count("ocr text") == 2
    assert probe.fitz_calls > 0
    assert probe.ocr_under_lock and not any(probe.ocr_under_lock)
