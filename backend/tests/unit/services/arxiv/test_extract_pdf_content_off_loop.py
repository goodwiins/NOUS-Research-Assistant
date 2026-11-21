"""GOO-642 / IS-6: the arXiv ingest pypdf parse must not run on the event loop.

``ArXivIngestionService.extract_pdf_content`` parsed PDFs of up to 50 MB with
pypdf inside ``async def``, stalling every other request on the worker.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict
from unittest.mock import patch

import fitz  # PyMuPDF, only to build a real PDF fixture
import pytest

from src.services.arxiv import arxiv_service as mod
from src.services.integrations import arxiv_fulltext


def _pdf_bytes(*pages: str) -> bytes:
    doc = fitz.open()
    for text in pages:
        doc.new_page().insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.unit
def test_sync_parse_keeps_page_framing() -> None:
    result = mod._extract_pdf_content_sync(_pdf_bytes("alpha page", "beta page"))

    assert result["num_pages"] == 2
    assert "[Page 1]\nalpha page" in result["full_text"]
    assert "[Page 2]\nbeta page" in result["full_text"]
    assert [p["page"] for p in result["page_texts"]] == [1, 2]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extract_pdf_content_does_not_block_the_loop(monkeypatch) -> None:
    # Thread pool stand-in for the worker process, as test_arxiv_fulltext does.
    monkeypatch.setattr(arxiv_fulltext, "_executor_factory", lambda: ThreadPoolExecutor(1))

    def _slow_parse(_content: bytes) -> Dict[str, Any]:
        time.sleep(0.5)
        return {"full_text": "x", "page_texts": [], "num_pages": 1, "metadata": {}}

    monkeypatch.setattr(mod, "_extract_pdf_content_sync", _slow_parse)

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    service = mod.ArXivIngestionService.__new__(mod.ArXivIngestionService)
    ticker = asyncio.create_task(_ticker())
    await asyncio.sleep(0)
    result = await service.extract_pdf_content(b"%PDF-")
    ticks_when_parse_returned = ticks
    await ticker

    assert result["full_text"] == "x"
    assert ticks_when_parse_returned >= 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_daemonic_process_parses_in_a_thread(monkeypatch) -> None:
    """A Celery prefork worker is daemonic and may not start a child process."""
    monkeypatch.setattr(mod.multiprocessing.current_process(), "daemon", True)
    with patch.object(arxiv_fulltext, "run_in_subprocess") as run_in_subprocess:
        result = await mod._parse_pdf_off_loop(_pdf_bytes("gamma"))

    run_in_subprocess.assert_not_called()
    assert "gamma" in result["full_text"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parse_failure_is_an_ingestion_error(monkeypatch) -> None:
    monkeypatch.setattr(arxiv_fulltext, "_executor_factory", lambda: ThreadPoolExecutor(1))
    service = mod.ArXivIngestionService.__new__(mod.ArXivIngestionService)
    with pytest.raises(mod.IngestionError):
        await service.extract_pdf_content(b"not a pdf")
