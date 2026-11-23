"""The multimodal entity step can succeed, and cannot fail the job (IS-5).

``extract_entities`` built ``EntityExtractionService(self.db)`` (the service
takes no arguments) and then awaited the synchronous
``extract_entities_from_text`` with keyword arguments it does not have. Either
TypeError fired for every document with text, and the step was required, so
``process_document`` failed the job and the document on every run and every
retry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.entity import Entity, EntityType, ExtractionMethod
from src.models.processing import JobStatus, ProcessingJob
from src.services.processing import multimodal_processing_service as mps
from src.services.processing.multimodal_processing_service import (
    MultimodalProcessingService,
)

pytestmark = pytest.mark.unit

TEXT = "Ada Lovelace worked with Charles Babbage in London on the engine."


@pytest.fixture(autouse=True)
def _fresh_entity_service(monkeypatch: pytest.MonkeyPatch) -> None:
    # The loaded service is cached per process; each test starts cold.
    monkeypatch.setattr(mps, "_entity_service_instance", None)


def _svc() -> MultimodalProcessingService:
    svc = object.__new__(MultimodalProcessingService)
    svc.db = MagicMock()
    svc.entity_service = None
    return svc


def _doc(text: str = TEXT) -> MagicMock:
    doc = MagicMock()
    doc.id = "doc-1"
    doc.organization_id = "org-1"
    doc.content_text = text
    return doc


class _RealSignatureService:
    """Same signatures as the real EntityExtractionService, without spaCy."""

    instances: List["_RealSignatureService"] = []

    def __init__(self) -> None:
        self.calls: List[Any] = []
        _RealSignatureService.instances.append(self)

    def extract_entities_from_text(self, document: Any, text: str) -> List[Entity]:
        self.calls.append((document, text))
        return [
            Entity(
                entity_type=EntityType.PERSON,
                name="Ada Lovelace",
                confidence=0.9,
                extraction_method=ExtractionMethod.SPACY,
                # spaCy entities carry start_char/end_char ...
                properties={"start_char": 4, "end_char": 16},
            ),
            Entity(
                entity_type=EntityType.EMAIL,
                name="ada@example.org",
                confidence=0.95,
                extraction_method=ExtractionMethod.REGEX,
                # ... the regex ones start/end.
                properties={"start": 20, "end": 35},
            ),
            Entity(
                entity_type=EntityType.LOCATION,
                name="London",
                confidence=0.8,
                extraction_method=ExtractionMethod.SPACY,
                properties=None,
            ),
        ]


@pytest.mark.asyncio
async def test_entity_step_returns_entities_with_the_real_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RealSignatureService.instances.clear()
    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    doc = _doc()

    result = await _svc().extract_entities(doc, MagicMock())

    assert result["entity_count"] == 3
    assert result["entities"][0] == {
        "text": "Ada Lovelace",
        "label": EntityType.PERSON.value,
        "confidence": 0.9,
        "start": 4,  # non-zero: 0 is also the default
        "end": 16,
    }
    assert (result["entities"][1]["start"], result["entities"][1]["end"]) == (20, 35)
    assert result["entities"][2]["start"] == 0  # no properties: defaults
    (svc_instance,) = _RealSignatureService.instances
    assert svc_instance.calls == [(doc, TEXT)]


@pytest.mark.asyncio
async def test_entity_step_runs_spacy_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread: List[bool] = []

    class _ThreadProbe(_RealSignatureService):
        def extract_entities_from_text(self, document: Any, text: str) -> List[Entity]:
            try:
                asyncio.get_running_loop()
                loop_thread.append(True)
            except RuntimeError:
                loop_thread.append(False)
            return []

    monkeypatch.setattr(mps, "EntityExtractionService", _ThreadProbe)

    await _svc().extract_entities(_doc(), MagicMock())

    assert loop_thread == [False]


@pytest.mark.asyncio
async def test_entity_step_skips_the_model_for_an_empty_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> None:
        raise AssertionError("spaCy must not load for an empty document")

    monkeypatch.setattr(mps, "EntityExtractionService", _boom)

    result = await _svc().extract_entities(_doc(""), MagicMock())

    assert result["entity_count"] == 0


def test_entity_step_is_not_required_in_any_pipeline() -> None:
    svc = _svc()
    for document_type in DocumentType:
        steps = svc.get_processing_pipeline(document_type)
        (entity_step,) = [s for s in steps if s.name == "Entity Extraction"]
        assert entity_step.required is False, document_type


@pytest.mark.asyncio
async def test_spacy_loads_off_the_loop_and_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: List[bool] = []

    class _Probe(_RealSignatureService):
        def __init__(self) -> None:
            try:
                asyncio.get_running_loop()
                loads.append(True)
            except RuntimeError:
                loads.append(False)
            super().__init__()

    monkeypatch.setattr(mps, "EntityExtractionService", _Probe)

    await _svc().extract_entities(_doc(), MagicMock())
    await _svc().extract_entities(_doc(), MagicMock())  # another service object

    assert loads == [False]  # loaded once, and not on the event loop


@pytest.mark.asyncio
async def test_a_failed_model_load_is_retried_by_the_next_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: List[int] = []

    def _flaky() -> _RealSignatureService:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("Can't find model 'en_core_web_sm'")
        return _RealSignatureService()

    monkeypatch.setattr(mps, "EntityExtractionService", _flaky)

    with pytest.raises(OSError):
        await _svc().extract_entities(_doc(), MagicMock())
    result = await _svc().extract_entities(_doc(), MagicMock())

    assert result["entity_count"] == 3


@pytest.mark.asyncio
async def test_entity_step_propagates_its_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Broken(_RealSignatureService):
        def extract_entities_from_text(self, document: Any, text: str) -> List[Entity]:
            raise ValueError("spaCy blew up")

    monkeypatch.setattr(mps, "EntityExtractionService", _Broken)

    # Not swallowed into an empty result: process_document must see it.
    with pytest.raises(ValueError, match="spaCy blew up"):
        await _svc().extract_entities(_doc(), MagicMock())


# --- the real finalisers ----------------------------------------------------


async def _ok(document: Any, job: Any) -> dict:
    return {}


def _real_doc(
    document_type: DocumentType = DocumentType.TEXT, content_text: Any = TEXT
) -> Document:
    return Document(
        id=uuid4(),
        organization_id=uuid4(),
        content_text=content_text,
        document_type=document_type,
        processing_status=ProcessingStatus.PROCESSING,
    )


def _pipeline_with(
    svc: MultimodalProcessingService,
    monkeypatch: pytest.MonkeyPatch,
    *,
    document_type: DocumentType = DocumentType.TEXT,
    broken: Optional[Dict[str, Any]] = None,
) -> None:
    """The production TEXT pipeline (names and required flags), every step
    stubbed to succeed except the real entity step and any ``broken`` one."""
    steps = MultimodalProcessingService.get_processing_pipeline(svc, document_type)
    broken = broken or {}
    for step in steps:
        if step.name in broken:
            step.function = broken[step.name]
        elif step.name != "Entity Extraction":
            step.function = _ok
    monkeypatch.setattr(svc, "get_processing_pipeline", lambda _type: steps)


@pytest.mark.asyncio
async def test_entity_failure_completes_the_document_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class _Broken(_RealSignatureService):
        def extract_entities_from_text(self, document: Any, text: str) -> List[Entity]:
            raise ValueError("spaCy blew up")

    monkeypatch.setattr(mps, "EntityExtractionService", _Broken)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(svc, monkeypatch)
    doc, job = _real_doc(), ProcessingJob()

    with caplog.at_level(logging.WARNING, logger=mps.logger.name):
        results = await svc.process_document(doc, job)

    assert results["success"] is True
    assert results["errors"] == []
    assert len(results["warnings"]) == 1
    assert "Entity Extraction" in results["warnings"][0]
    assert doc.processing_status == ProcessingStatus.COMPLETED
    assert doc.is_indexed is True
    assert doc.get_metadata()["processing_partial"] is True
    assert doc.get_metadata()["processing_warnings"] == results["warnings"]
    assert job.status == JobStatus.COMPLETED
    # Logged once, with the stack.
    logged = [r for r in caplog.records if "Entity Extraction" in r.getMessage()]
    assert len(logged) == 1 and logged[0].exc_info is not None


@pytest.mark.asyncio
async def test_a_clean_run_records_no_warnings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(svc, monkeypatch)
    doc, job = _real_doc(), ProcessingJob()
    doc.add_metadata("processing_warnings", ["stale"])
    doc.add_metadata("processing_partial", True)

    await svc.process_document(doc, job)

    assert doc.get_metadata()["processing_warnings"] == []
    assert doc.get_metadata()["processing_partial"] is False
    assert job.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_required_step_failure_still_fails_document_and_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def _broken(document: Any, job: Any) -> dict:
        raise RuntimeError("cannot read file")

    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(svc, monkeypatch, broken={"Text Extraction": _broken})
    doc, job = _real_doc(), ProcessingJob()

    results = await svc.process_document(doc, job)

    assert results["success"] is False
    assert any("Text Extraction" in e for e in results["errors"])
    assert doc.processing_status == ProcessingStatus.FAILED
    assert job.status == JobStatus.FAILED


@pytest.mark.asyncio
async def test_concurrent_first_calls_load_spacy_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: List[int] = []

    class _Slow(_RealSignatureService):
        def __init__(self) -> None:
            loads.append(1)
            time.sleep(0.2)  # long enough for every caller to arrive
            super().__init__()

    monkeypatch.setattr(mps, "EntityExtractionService", _Slow)

    results = await asyncio.gather(
        *(_svc().extract_entities(_doc(), MagicMock()) for _ in range(5))
    )

    assert loads == [1]
    assert all(r["entity_count"] == 3 for r in results)


# --- usable text, stable messages, stale flags ------------------------------

SECRET = "/opt/venv/bin/tesseract: libleptonica.so.5 missing at 10.0.3.7"


async def _leaky(document: Any, job: Any) -> dict:
    raise RuntimeError(SECRET)


@pytest.mark.asyncio
async def test_a_scanned_pdf_whose_ocr_fails_ends_failed_not_indexed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(
        svc,
        monkeypatch,
        document_type=DocumentType.PDF,
        broken={"OCR Extraction": _leaky},
    )
    doc = _real_doc(DocumentType.PDF, "[Page 1]\n\n[Page 2]\n  \n")
    job = ProcessingJob()

    results = await svc.process_document(doc, job)

    assert results["success"] is False
    assert doc.processing_status == ProcessingStatus.FAILED
    assert not doc.is_indexed
    assert doc.processing_error == mps._NO_TEXT_ERROR
    assert job.status == JobStatus.FAILED
    assert doc.get_metadata()["processing_partial"] is False


@pytest.mark.asyncio
async def test_an_image_whose_ocr_fails_ends_failed_not_indexed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(
        svc, monkeypatch, document_type=DocumentType.IMAGE, broken={"Image OCR": _leaky}
    )
    doc, job = _real_doc(DocumentType.IMAGE, None), ProcessingJob()

    await svc.process_document(doc, job)

    assert doc.processing_status == ProcessingStatus.FAILED
    assert not doc.is_indexed
    assert job.status == JobStatus.FAILED


@pytest.mark.asyncio
async def test_a_scanned_pdf_keeps_the_text_ocr_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def _ocr(document: Any, job: Any) -> dict:
        return {"ocr_text": "Scanned words"}

    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(
        svc,
        monkeypatch,
        document_type=DocumentType.PDF,
        broken={"OCR Extraction": _ocr},
    )
    doc, job = _real_doc(DocumentType.PDF, "[Page 1]\n"), ProcessingJob()

    await svc.process_document(doc, job)

    assert doc.processing_status == ProcessingStatus.COMPLETED
    assert doc.is_indexed is True
    assert doc.content_text == "Scanned words"


@pytest.mark.asyncio
async def test_a_pdf_with_text_completes_when_ocr_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(
        svc,
        monkeypatch,
        document_type=DocumentType.PDF,
        broken={"OCR Extraction": _leaky},
    )
    doc, job = _real_doc(DocumentType.PDF, "[Page 1]\n" + TEXT), ProcessingJob()

    await svc.process_document(doc, job)

    assert doc.processing_status == ProcessingStatus.COMPLETED
    assert doc.get_metadata()["processing_partial"] is True


@pytest.mark.asyncio
async def test_no_exception_text_reaches_the_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(
        svc,
        monkeypatch,
        document_type=DocumentType.PDF,
        broken={"OCR Extraction": _leaky},
    )
    warned = _real_doc(DocumentType.PDF, "[Page 1]\n" + TEXT)
    await svc.process_document(warned, ProcessingJob())

    svc2 = _svc()
    svc2.processed_dir = tmp_path
    _pipeline_with(
        svc2,
        monkeypatch,
        document_type=DocumentType.PDF,
        broken={"PDF Processing": _leaky},
    )
    failed, failed_job = _real_doc(DocumentType.PDF, TEXT), ProcessingJob()
    await svc2.process_document(failed, failed_job)

    assert warned.get_metadata()["processing_warnings"] == ["OCR Extraction failed"]
    assert failed.processing_error == "Processing step 'PDF Processing' failed"
    for doc in (warned, failed):
        assert SECRET not in repr(doc.get_metadata())
        assert SECRET not in (doc.processing_error or "")
    assert SECRET not in repr(failed_job.error_message)


@pytest.mark.asyncio
async def test_a_required_failure_replaces_stale_partial_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(svc, monkeypatch, broken={"Text Extraction": _leaky})
    doc = _real_doc()
    doc.add_metadata("processing_warnings", ["AI Analysis failed"])
    doc.add_metadata("processing_partial", True)

    await svc.process_document(doc, ProcessingJob())

    assert doc.processing_status == ProcessingStatus.FAILED
    assert doc.get_metadata()["processing_partial"] is False
    assert doc.get_metadata()["processing_warnings"] == []


@pytest.mark.asyncio
async def test_an_unexpected_crash_replaces_stale_partial_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(svc, monkeypatch)

    async def _boom(document: Any, results: Any) -> None:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(svc, "update_document_with_results", _boom)
    doc, job = _real_doc(), ProcessingJob()
    doc.add_metadata("processing_partial", True)

    results = await svc.process_document(doc, job)

    assert doc.get_metadata()["processing_partial"] is False
    assert results["errors"] == ["Document processing failed"]
    assert SECRET not in repr(job.error_message)


# --- steps that catch their own errors leak nothing either -------------------


def _strings(value: Any) -> List[str]:
    """Every string anywhere inside ``value`` (dict keys and values, lists)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [x for k, v in value.items() for x in _strings(k) + _strings(v)]
    if isinstance(value, (list, tuple)):
        return [x for item in value for x in _strings(item)]
    return []


def _assert_no_secret(*stores: Any) -> None:
    leaked = [text for store in stores for text in _strings(store) if SECRET in text]
    assert leaked == []


@pytest.mark.asyncio
async def test_internally_caught_step_errors_leak_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from src.services.documents import document_quality_service as dqs
    from src.services.knowledge_graph import knowledge_graph_service as kgs
    from src.services.processing import entity_extraction_service as ees

    entity = Entity(
        entity_type=EntityType.PERSON,
        name="Ada Lovelace",
        confidence=0.9,
        extraction_method=ExtractionMethod.SPACY,
        properties={"start_char": 0, "end_char": 12},
    )

    class _Spacy:
        def nlp(self, text: str) -> Any:
            return object()

        def extract_entities_from_text(self, document: Any, text: str) -> List[Any]:
            return [entity]

        def _extract_relationships(self, doc: Any, entities: Any) -> List[Any]:
            raise RuntimeError(SECRET)

    class _Quality:
        def __init__(self, db: Any) -> None:
            pass

        async def comprehensive_quality_assessment(self, document: Any) -> dict:
            raise RuntimeError(SECRET)

    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    monkeypatch.setattr(ees, "EntityExtractionService", _Spacy)
    monkeypatch.setattr(
        kgs.knowledge_graph_service,
        "create_entity",
        AsyncMock(side_effect=RuntimeError(SECRET)),
    )
    monkeypatch.setattr(dqs, "DocumentQualityService", _Quality)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(
        svc,
        monkeypatch,
        broken={
            # The real steps: they catch their own errors and return them.
            "Knowledge Graph Storage": svc.store_entities_in_knowledge_graph,
            "Quality Assessment": svc.assess_quality,
        },
    )
    doc, job = _real_doc(), ProcessingJob()

    results = await svc.process_document(doc, job)

    assert doc.processing_status == ProcessingStatus.COMPLETED
    kg = results["knowledge_graph_storage"]
    assert kg["errors"] == [
        mps._KG_ENTITY_ERROR,
        mps._KG_EXTRACTION_ERROR,
    ]
    assert results["quality_assessment"]["error"] == mps._QUALITY_ERROR
    _assert_no_secret(results, doc.get_metadata(), job.result, job.error_message)


@pytest.mark.asyncio
async def test_a_knowledge_graph_crash_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from src.services.processing import entity_extraction_service as ees

    def _crash() -> Any:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    monkeypatch.setattr(ees, "EntityExtractionService", _crash)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(
        svc,
        monkeypatch,
        broken={"Knowledge Graph Storage": svc.store_entities_in_knowledge_graph},
    )
    doc, job = _real_doc(), ProcessingJob()

    results = await svc.process_document(doc, job)

    assert results["knowledge_graph_storage"]["errors"] == [mps._KG_STORAGE_ERROR]
    _assert_no_secret(results, doc.get_metadata(), job.result)


def test_retry_task_failure_leaks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tasks import document_processing_tasks as dpt

    job = ProcessingJob()
    setattr(job, "retry_count", 0)
    setattr(job, "max_retries", 3)
    job.fail_job("earlier failure")
    doc = _real_doc()

    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job, doc]
    monkeypatch.setattr(dpt, "SessionLocal", lambda: db)

    class _Service:
        def __init__(self, db: Any) -> None:
            pass

        async def process_document(self, document: Any, job: Any) -> dict:
            raise PermissionError(13, "Permission denied", SECRET)

    monkeypatch.setattr(dpt, "MultimodalProcessingService", _Service)

    result = dpt.retry_failed_processing.run(str(uuid4()))

    assert result["status"] == "error"
    assert result["error"] == dpt._RETRY_FAILED
    assert job.status == JobStatus.FAILED
    assert job.error_message == dpt._RETRY_FAILED
    _assert_no_secret(result, job.error_message, job.result)
