"""A multi-document KG extraction must give each document its own task budget."""

from types import SimpleNamespace
from typing import Any, Mapping, cast
from unittest.mock import MagicMock

import pytest

from src.models.document import Document
from src.models.processing import JobStatus, ProcessingJob
from src.tasks import processing_tasks as pt

pytestmark = pytest.mark.unit


def test_multi_document_job_dispatches_one_task_per_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = MagicMock()
    job.status = JobStatus.QUEUED
    job.parameters = {"document_ids": ["doc-1", "doc-2"]}
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = job
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)

    scheduled: dict[str, list[Any]] = {}

    def fake_group(tasks: list[Any]) -> SimpleNamespace:
        scheduled["tasks"] = tasks
        return SimpleNamespace(apply_async=lambda: SimpleNamespace(id="group-1"))

    monkeypatch.setattr(pt, "group", fake_group, raising=False)
    monkeypatch.setattr(
        pt,
        "LLMEntityExtractionService",
        lambda: pytest.fail("batch dispatcher ran document extraction inline"),
    )

    result = pt.kg_extract_entities_job.apply(args=("job-1",)).result

    assert result == {"status": "running", "job_id": "job-1"}
    assert len(scheduled["tasks"]) == 2
    assert [task.args for task in scheduled["tasks"]] == [
        ("job-1", 0),
        ("job-1", 1),
    ]
    assert pt.kg_extract_entities_job.soft_time_limit > 300
    job.start_job.assert_called_once()
    job.complete_job.assert_not_called()


def test_redelivered_single_document_job_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live one-document task can be retried after its worker dies."""
    job = MagicMock()
    job.status = JobStatus.RUNNING
    job.parameters = {"document_ids": ["doc-1"]}
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job, None]
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)

    result = pt.kg_extract_entities_job.apply(args=("job-1",)).result

    assert result["status"] == "failed"
    job.start_job.assert_called_once()


def test_redelivered_batch_dispatcher_does_not_publish_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation check: disable the RUNNING batch guard in
    processing_tasks.py and run this test; it must attempt a second publish.
    """
    job = MagicMock()
    job.status = JobStatus.RUNNING
    job.parameters = {"document_ids": ["doc-1", "doc-2"]}
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = job
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)
    monkeypatch.setattr(pt, "group", lambda tasks: pytest.fail("published batch twice"))

    result = pt.kg_extract_entities_job.apply(args=("job-1",)).result

    assert result["skipped"] == "duplicate_delivery"
    job.start_job.assert_not_called()


def test_retry_clears_saved_document_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation check: omit the RETRYING result clear in processing_tasks.py;
    this test must observe the previous attempt's document checkpoint.
    """
    job = MagicMock()
    job.status = JobStatus.RETRYING
    job.parameters = {"document_ids": ["doc-1", "doc-2"]}
    job.result = {"_kg_document_results": {"0": {"entities_created": 1}}}
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = job
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        pt,
        "group",
        lambda tasks: SimpleNamespace(
            apply_async=lambda: SimpleNamespace(id="group-2")
        ),
    )

    result = pt.kg_extract_entities_job.apply(args=("job-1",)).result

    assert result["status"] == "running"
    assert job.result is None


def _document_job(
    monkeypatch: pytest.MonkeyPatch, *, documents: Mapping[str, SimpleNamespace | None]
) -> MagicMock:
    job = MagicMock()
    job.status = JobStatus.RUNNING
    job.parameters = {"document_ids": list(documents)}
    job.result = None

    def new_session() -> MagicMock:
        db = MagicMock()
        job_query = MagicMock()
        job_query.filter.return_value.first.return_value = job
        doc_query = MagicMock()

        def document_filter(condition: Any) -> MagicMock:
            doc_query.filter.return_value.first.return_value = documents.get(
                condition.right.value
            )
            return cast(MagicMock, doc_query.filter.return_value)

        doc_query.filter.side_effect = document_filter
        db.query.side_effect = lambda model: (
            job_query if model is ProcessingJob else doc_query
        )
        return db

    monkeypatch.setattr(pt, "SessionLocal", new_session)
    return job


def test_document_tasks_accumulate_results_and_skip_redelivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation check: disable the ``prior is not None`` guard in
    processing_tasks.py and run this test; the LLM call count must become 3.
    """
    documents = {
        doc_id: SimpleNamespace(
            id=doc_id, organization_id="org-1", content_text=f"Text {doc_id}"
        )
        for doc_id in ("doc-1", "doc-2")
    }
    job = _document_job(monkeypatch, documents=documents)
    extraction = SimpleNamespace(
        entities=[
            SimpleNamespace(
                name="BERT",
                type="concept",
                confidence=0.9,
                description=None,
                aliases=[],
            )
        ],
        relationships=[],
        error=None,
        chunks_failed=0,
    )
    service = MagicMock()
    service.extract_entities.return_value = extraction
    monkeypatch.setattr(pt, "LLMEntityExtractionService", lambda: service)
    monkeypatch.setattr(pt, "run_async", lambda value: value)
    kg = MagicMock()
    kg.create_entities_batch.return_value = SimpleNamespace(
        created_entities=[SimpleNamespace(id="node-1", name="BERT")],
        created_relationships=[],
        errors=[],
    )
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)

    first = pt.kg_extract_entities_job.apply(args=("job-1", 0)).result
    repeated = pt.kg_extract_entities_job.apply(args=("job-1", 0)).result
    assert (
        service.extract_entities.call_count == 1
    ), "redelivery repeated paid extraction"
    last = pt.kg_extract_entities_job.apply(args=("job-1", 1)).result

    assert first["status"] == "running"
    assert repeated["skipped"] == "duplicate_delivery"
    assert last["status"] == "completed"
    assert service.extract_entities.call_count == 2
    assert job.complete_job.call_args.kwargs["result"] == {
        "document_ids": ["doc-1", "doc-2"],
        "entities_found": 2,
        "entities_created": 2,
        "relationships_found": 0,
        "relationships_created": 0,
        "errors": 0,
        "extraction_errors": 0,
    }


def test_all_documents_missing_fails_the_aggregate_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _document_job(monkeypatch, documents={"doc-1": None, "doc-2": None})

    first = pt.kg_extract_entities_job.apply(args=("job-1", 0)).result
    last = pt.kg_extract_entities_job.apply(args=("job-1", 1)).result

    assert first["status"] == "running"
    assert last["status"] == "failed"
    assert last["result"]["errors"] == 2
    job.fail_job.assert_called_once_with(
        "KG extraction created no entities or relationships (2 errors across 2 documents)",
        error_type="extraction_failed",
    )
    assert job.result is None
    job.complete_job.assert_not_called()


def test_one_missing_document_preserves_successful_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _document_job(
        monkeypatch,
        documents={
            "doc-1": SimpleNamespace(
                id="doc-1", organization_id="org-1", content_text="BERT"
            ),
            "doc-2": None,
        },
    )
    service = MagicMock()
    service.extract_entities.return_value = SimpleNamespace(
        entities=[
            SimpleNamespace(
                name="BERT",
                type="concept",
                confidence=0.9,
                description=None,
                aliases=[],
            )
        ],
        relationships=[],
        error="some chunks timed out",
        chunks_failed=0,
    )
    monkeypatch.setattr(pt, "LLMEntityExtractionService", lambda: service)
    monkeypatch.setattr(pt, "run_async", lambda value: value)
    kg = MagicMock()
    kg.create_entities_batch.return_value = SimpleNamespace(
        created_entities=[SimpleNamespace(id="node-1", name="BERT")],
        created_relationships=[],
        errors=[],
    )
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)

    pt.kg_extract_entities_job.apply(args=("job-1", 0))
    last = pt.kg_extract_entities_job.apply(args=("job-1", 1)).result

    assert last["status"] == "completed"
    assert job.complete_job.call_args.kwargs["result"]["entities_created"] == 1
    assert job.complete_job.call_args.kwargs["result"]["errors"] == 1
    assert job.complete_job.call_args.kwargs["result"]["extraction_errors"] == 1
    job.fail_job.assert_not_called()


def test_document_exception_fails_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _document_job(
        monkeypatch,
        documents={
            "doc-1": None,
            "doc-2": SimpleNamespace(
                id="doc-2", organization_id="org-1", content_text="BERT"
            ),
        },
    )
    monkeypatch.setattr(pt, "LLMEntityExtractionService", MagicMock())
    monkeypatch.setattr(
        pt, "run_async", MagicMock(side_effect=RuntimeError("LLM unavailable"))
    )

    pt.kg_extract_entities_job.run("job-1", 0)
    with pytest.raises(RuntimeError, match="LLM unavailable"):
        pt.kg_extract_entities_job.run("job-1", 1)

    job.fail_job.assert_called_once_with("LLM unavailable", error_type="RuntimeError")
    assert job.result is None
    job.complete_job.assert_not_called()
