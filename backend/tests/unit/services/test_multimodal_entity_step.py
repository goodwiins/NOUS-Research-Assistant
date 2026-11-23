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
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock
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
                properties={"start_char": 0, "end_char": 12},
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
        "start": 0,
        "end": 12,
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


def _real_doc() -> Document:
    return Document(
        id=uuid4(),
        organization_id=uuid4(),
        content_text=TEXT,
        document_type=DocumentType.TEXT,
        processing_status=ProcessingStatus.PROCESSING,
    )


def _pipeline_with(
    svc: MultimodalProcessingService, monkeypatch: pytest.MonkeyPatch, **broken: Any
) -> None:
    """The production TEXT pipeline (names and required flags), every step
    stubbed to succeed except the real entity step and any ``broken`` one."""
    steps = MultimodalProcessingService.get_processing_pipeline(svc, DocumentType.TEXT)
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
    _pipeline_with(svc, monkeypatch, **{"Text Extraction": _broken})
    doc, job = _real_doc(), ProcessingJob()

    results = await svc.process_document(doc, job)

    assert results["success"] is False
    assert any("Text Extraction" in e for e in results["errors"])
    assert doc.processing_status == ProcessingStatus.FAILED
    assert job.status == JobStatus.FAILED
