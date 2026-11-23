"""GOO-695: the local arXiv PDF read stays off the event loop.

``_extract_features_from_local_pdfs`` parses each PDF with PyMuPDF, which is
CPU-bound. GOO-643 moved the read to a worker thread, but no test failed if
it moved back onto the loop.
"""

import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from src.api.arxiv import arxiv_local as mod

pytestmark = pytest.mark.unit


async def test_pdf_read_runs_in_a_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "2401.00001.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(mod, "ARXIV_DATA_PATH", tmp_path)

    loop_thread = threading.current_thread()
    reads: List[threading.Thread] = []

    def _read(pdf_file: Path) -> Tuple[str, Dict[str, Any]]:
        reads.append(threading.current_thread())
        return "text " * 50, {"title": "T", "page_count": 1}

    monkeypatch.setattr(mod, "_read_pdf_text_and_metadata", _read)

    request = mod.LocalExtractionRequest(
        paper_ids=None,
        extract_entities=False,
        extract_topics=False,
        extract_keyphrases=False,
        extract_summaries=False,
        update_knowledge_graph=False,
    )
    response = await mod._extract_features_from_local_pdfs(
        request, background_tasks=None, current_user={}  # type: ignore[arg-type]
    )

    assert response.processed_count == 1
    assert len(reads) == 1
    assert reads[0] is not loop_thread, "the PDF read ran on the event loop"
