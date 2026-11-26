"""Multimodal pipeline failures end visible and terminal (GOO-743).

- Knowledge Graph Storage used to catch its own errors and report success,
  leaving ``neo4j_index_status`` unset, so the reconciler (which re-drives
  COMPLETED documents whose graph status is ``failed``) never retried it.
- The outer ``except`` left the document in PROCESSING.
- A failed or crashed rerun kept the previous run's ``processing_results`` and
  ``is_indexed``.
- ``DocumentQualityService`` stored raw exception text, and kept
  ``QualityIssue`` objects (not JSON) in ``dimension_scores``.

Rule from #2253/#2272: a failure is stored as a fixed, safe message; the
detail goes to the log with ``exc_info``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from src.models.document import DocumentType, ProcessingStatus
from src.models.entity import Entity, EntityType, ExtractionMethod
from src.models.processing import JobStatus, ProcessingJob
from src.services.processing import multimodal_processing_service as mps
from src.shared.enums import SatelliteSyncStatus
from tests.unit.services.test_multimodal_entity_step import (
    SECRET,
    TEXT,
    _assert_no_secret,
    _pipeline_with,
    _real_doc,
    _RealSignatureService,
    _svc,
)

pytestmark = pytest.mark.unit

KG = "Knowledge Graph Storage"
FAILED = SatelliteSyncStatus.FAILED.value
COMPLETED = SatelliteSyncStatus.COMPLETED.value


def _entity(name: str, start: int) -> Entity:
    entity = Entity(
        entity_type=EntityType.PERSON,
        name=name,
        confidence=0.9,
        extraction_method=ExtractionMethod.SPACY,
        properties={"start_char": start, "end_char": start + len(name)},
    )
    entity.id = name  # the step maps local ids to graph ids
    return entity


ADA, BABBAGE = _entity("Ada Lovelace", 0), _entity("Charles Babbage", 25)


def _spacy(relationships: List[Any] | Exception) -> type:
    class _Spacy:
        def nlp(self, text: str) -> Any:
            return object()

        def extract_entities_from_text(self, document: Any, text: str) -> List[Any]:
            return [ADA, BABBAGE]

        def _extract_relationships(self, doc: Any, entities: Any) -> List[Any]:
            if isinstance(relationships, Exception):
                raise relationships
            return relationships

    return _Spacy


def _kg_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    relationships: List[Any] | Exception,
    create_entity: Any,
    create_relationship: Any = None,
) -> tuple[Any, Any, Any, Any]:
    """The production TEXT pipeline with the real KG step; returns
    (service, document, job, coroutine)."""
    from src.services.knowledge_graph import knowledge_graph_service as kgs
    from src.services.processing import entity_extraction_service as ees

    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    monkeypatch.setattr(ees, "EntityExtractionService", _spacy(relationships))
    monkeypatch.setattr(kgs.knowledge_graph_service, "create_entity", create_entity)
    monkeypatch.setattr(
        kgs.knowledge_graph_service,
        "create_relationship",
        create_relationship or MagicMock(),
    )
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(svc, monkeypatch, broken={KG: svc.store_entities_in_knowledge_graph})
    doc, job = _real_doc(), ProcessingJob()
    return svc, doc, job, svc.process_document(doc, job)


def _graph_ids(request: Any) -> Any:
    return MagicMock(id=f"graph-{request.name}")


def _assert_reconciler_picks_up(doc: Any) -> None:
    """``reconcile_tasks``: COMPLETED, not deleted, neo4j_index_status=failed."""
    assert doc.processing_status == ProcessingStatus.COMPLETED
    assert not doc.is_deleted
    assert doc.neo4j_index_status == FAILED


def _assert_kg_warning(results: Any, doc: Any) -> None:
    assert results["warnings"] == [f"{KG} failed"]
    assert KG.lower().replace(" ", "_") not in results  # no success-shaped result
    assert doc.get_metadata()["processing_warnings"] == [f"{KG} failed"]
    assert doc.get_metadata()["processing_partial"] is True


# --- Knowledge Graph Storage reports its failure -----------------------------


@pytest.mark.asyncio
async def test_an_entity_write_error_leaves_the_graph_failed_for_the_reconciler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _svc_, doc, job, run = _kg_run(
        monkeypatch,
        tmp_path,
        relationships=[],
        create_entity=MagicMock(side_effect=RuntimeError(SECRET)),
    )
    with caplog.at_level(logging.WARNING, logger=mps.logger.name):
        results = await run

    _assert_reconciler_picks_up(doc)
    _assert_kg_warning(results, doc)
    assert job.status == JobStatus.COMPLETED
    assert any(r.exc_info for r in caplog.records if "entity" in r.getMessage())
    _assert_no_secret(results, doc.get_metadata(), job.result, doc.processing_error)


@pytest.mark.asyncio
async def test_a_relationship_write_error_leaves_the_graph_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Reaches the per-relationship ``except``: both entities land, the edge
    between them does not."""
    relationship = {
        "source_entity": ADA,
        "target_entity": BABBAGE,
        "relationship_type": "knows",
        "evidence": "worked with",
    }
    create_relationship = MagicMock(side_effect=RuntimeError(SECRET))
    _svc_, doc, job, run = _kg_run(
        monkeypatch,
        tmp_path,
        relationships=[relationship],
        create_entity=MagicMock(side_effect=_graph_ids),
        create_relationship=create_relationship,
    )
    with caplog.at_level(logging.WARNING, logger=mps.logger.name):
        results = await run

    assert create_relationship.call_count == 1
    _assert_reconciler_picks_up(doc)
    _assert_kg_warning(results, doc)
    assert any(r.exc_info for r in caplog.records if "relationship" in r.getMessage())
    _assert_no_secret(results, doc.get_metadata(), job.result)


@pytest.mark.asyncio
async def test_a_relationship_extraction_error_leaves_the_graph_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _svc_, doc, job, run = _kg_run(
        monkeypatch,
        tmp_path,
        relationships=RuntimeError(SECRET),
        create_entity=MagicMock(side_effect=_graph_ids),
    )
    results = await run

    _assert_reconciler_picks_up(doc)
    _assert_kg_warning(results, doc)
    _assert_no_secret(results, doc.get_metadata(), job.result)


@pytest.mark.asyncio
async def test_a_clean_graph_write_marks_the_graph_completed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    relationship = {
        "source_entity": ADA,
        "target_entity": BABBAGE,
        "relationship_type": "knows",
    }
    _svc_, doc, job, run = _kg_run(
        monkeypatch,
        tmp_path,
        relationships=[relationship],
        create_entity=MagicMock(side_effect=_graph_ids),
    )
    doc.neo4j_index_status = FAILED  # an earlier run's failure

    results = await run

    assert doc.neo4j_index_status == COMPLETED
    assert doc.neo4j_indexed_at is not None
    assert results["warnings"] == []
    stored = results["knowledge_graph_storage"]
    assert (stored["entities_stored"], stored["relationships_stored"]) == (2, 1)


# --- the document always ends terminal, with this run's results --------------


@pytest.mark.asyncio
async def test_an_unexpected_crash_fails_the_document_with_a_fixed_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(svc, monkeypatch)

    async def _boom(document: Any, results: Any) -> None:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(svc, "update_document_with_results", _boom)
    doc, job = _real_doc(), ProcessingJob()
    doc.is_indexed = True
    doc.add_metadata("processing_results", {"success": True, "from": "last run"})

    with caplog.at_level(logging.ERROR, logger=mps.logger.name):
        await svc.process_document(doc, job)

    assert doc.processing_status == ProcessingStatus.FAILED
    assert doc.processing_error == "Document processing failed"
    assert doc.is_indexed is False
    assert doc.get_metadata()["processing_results"]["success"] is False
    assert "from" not in doc.get_metadata()["processing_results"]
    assert job.status == JobStatus.FAILED
    svc.db.rollback.assert_called()  # the session may be the thing that broke
    assert any(r.exc_info for r in caplog.records)
    _assert_no_secret(doc.get_metadata(), job.error_message, doc.processing_error)


@pytest.mark.asyncio
async def test_a_required_failure_replaces_the_previous_runs_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def _broken(document: Any, job: Any) -> dict:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(svc, monkeypatch, broken={"Embedding Generation": _broken})
    doc, job = _real_doc(), ProcessingJob()
    doc.is_indexed = True
    doc.add_metadata("processing_results", {"success": True, "from": "last run"})

    results = await svc.process_document(doc, job)

    assert doc.processing_status == ProcessingStatus.FAILED
    assert doc.is_indexed is False
    stored = doc.get_metadata()["processing_results"]
    assert stored["success"] is False and "from" not in stored
    assert stored["errors"] == results["errors"]
    _assert_no_secret(doc.get_metadata(), job.error_message)


@pytest.mark.asyncio
async def test_the_no_text_guard_clears_an_earlier_runs_indexed_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(svc, monkeypatch, document_type=DocumentType.IMAGE)
    doc, job = _real_doc(DocumentType.IMAGE, None), ProcessingJob()
    doc.is_indexed = True

    await svc.process_document(doc, job)

    assert doc.processing_status == ProcessingStatus.FAILED
    assert doc.is_indexed is False


@pytest.mark.asyncio
async def test_a_step_result_that_is_not_json_still_completes_the_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The JSON columns would reject it at commit, stranding the document."""

    class _Opaque:
        def __repr__(self) -> str:
            return SECRET

    async def _odd(document: Any, job: Any) -> dict:
        return {
            "value": _Opaque(),
            "at": datetime.now(timezone.utc),
            "kind": DocumentType.PDF,
        }

    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(svc, monkeypatch, broken={"AI Analysis": _odd})
    doc, job = _real_doc(), ProcessingJob()

    await svc.process_document(doc, job)

    assert doc.processing_status == ProcessingStatus.COMPLETED
    json.dumps(doc.get_metadata())
    json.dumps(job.result)
    _assert_no_secret(doc.get_metadata(), job.result)


# --- DocumentQualityService --------------------------------------------------


def _quality_doc(text: str = TEXT) -> Any:
    doc = _real_doc(DocumentType.PDF, text)
    doc.title = "t"
    doc.filename = "t.pdf"
    doc.file_size_bytes = 10
    doc.mime_type = "application/pdf"
    return doc


@pytest.mark.asyncio
async def test_every_quality_assessor_failure_stores_a_fixed_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The real service, every dimension raising: nothing it returns carries
    the exception text, and all of it is JSON."""
    from src.services.documents import document_quality_service as dqs

    def _len(_value: Any) -> int:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(dqs, "len", _len, raising=False)
    service = dqs.DocumentQualityService(MagicMock())

    with caplog.at_level(logging.ERROR, logger=dqs.logger.name):
        assessment = await service.comprehensive_quality_assessment(_quality_doc())

    json.dumps(assessment)
    failed = {
        name
        for name, result in assessment["dimension_scores"].items()
        if any(i["description"] == dqs.ASSESSMENT_FAILED for i in result["issues"])
    }
    assert failed == {
        "readability",
        "coherence",
        "completeness",
        "accuracy",
        "technical_quality",
        "content_quality",
        "metadata_quality",
    }
    assert any(r.exc_info for r in caplog.records)
    _assert_no_secret(assessment)


@pytest.mark.asyncio
async def test_a_quality_service_crash_stores_a_fixed_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.documents import document_quality_service as dqs

    service = dqs.DocumentQualityService(MagicMock())
    monkeypatch.setattr(
        service,
        "generate_comprehensive_recommendations",
        MagicMock(side_effect=RuntimeError(SECRET)),
    )
    monkeypatch.setattr(
        service,
        "quick_readability_assessment",
        MagicMock(side_effect=RuntimeError(SECRET)),
    )

    full = await service.comprehensive_quality_assessment(_quality_doc())
    quick = await service.quick_quality_assessment(_quality_doc())

    assert full["error"] == quick["error"] == dqs.QUALITY_FAILED
    _assert_no_secret(full, quick)


@pytest.mark.asyncio
async def test_real_quality_issues_reach_the_document_as_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A short text makes the real assessors raise real QualityIssues; the
    document still completes and stores them as plain dicts."""
    from src.services.documents import document_quality_service as dqs

    monkeypatch.setattr(mps, "EntityExtractionService", _RealSignatureService)
    svc = _svc()
    svc.processed_dir = tmp_path
    _pipeline_with(svc, monkeypatch, broken={"Quality Assessment": svc.assess_quality})
    monkeypatch.setattr(dqs.DocumentQualityService, "store_quality_metrics", _noop)
    doc, job = _quality_doc("Too short."), ProcessingJob()

    results = await svc.process_document(doc, job)

    assert doc.processing_status == ProcessingStatus.COMPLETED
    quality = doc.get_metadata()["processing_results"]["quality_assessment"]
    issues = [i for r in quality["dimension_scores"].values() for i in r["issues"]]
    assert issues and all(isinstance(i, dict) for i in issues)
    json.dumps(doc.get_metadata())
    json.dumps(job.result)
    assert results["success"] is True


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None
