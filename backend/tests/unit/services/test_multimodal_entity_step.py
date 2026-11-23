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
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from src.models.document import DocumentType
from src.models.entity import Entity, EntityType, ExtractionMethod
from src.services.processing import multimodal_processing_service as mps
from src.services.processing.multimodal_processing_service import (
    MultimodalProcessingService,
)

pytestmark = pytest.mark.unit

TEXT = "Ada Lovelace worked with Charles Babbage in London on the engine."


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
                properties={"start": 0, "end": 12},
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

    assert result["entity_count"] == 2
    assert result["entities"][0] == {
        "text": "Ada Lovelace",
        "label": EntityType.PERSON.value,
        "confidence": 0.9,
        "start": 0,
        "end": 12,
    }
    assert result["entities"][1]["start"] == 0  # no properties: defaults
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
async def test_entity_failure_does_not_fail_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _svc()
    doc, job = _doc(), MagicMock()

    async def _ok(document: Any, job: Any) -> dict:
        return {}

    async def _broken(document: Any, job: Any) -> dict:
        raise OSError("Can't find model 'en_core_web_sm'")

    def _pipeline(document_type: Any) -> list:
        return [
            mps.ProcessingStep("Text Extraction", _ok),
            mps.ProcessingStep("Entity Extraction", _broken, required=False),
            mps.ProcessingStep("Embedding Generation", _ok),
        ]

    real = MultimodalProcessingService.get_processing_pipeline
    steps = real(svc, doc.document_type)
    (entity_step,) = [s for s in steps if s.name == "Entity Extraction"]
    # Mirror the production flag, so this test fails if it flips back.
    _pipeline_steps = _pipeline(None)
    _pipeline_steps[1].required = entity_step.required

    monkeypatch.setattr(svc, "get_processing_pipeline", lambda t: _pipeline_steps)
    finished: List[Any] = []

    async def _update(document: Any, results: Any) -> None:
        finished.append(results)

    monkeypatch.setattr(svc, "update_document_with_results", _update)
    monkeypatch.setattr(svc, "_stage_for_bedrock", lambda d, r: None)
    monkeypatch.setattr(svc, "_finalize_job", lambda j, d, r: None)

    await svc.process_document(doc, job)

    job.fail_job.assert_not_called()
    assert finished, "the pipeline returned before finishing the document"
    assert any("Entity Extraction" in e for e in finished[0]["errors"])
