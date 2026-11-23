"""GOO-743 review round: the multimodal pipeline against a real SQLAlchemy
session (SQLite), where a mock cannot fail the way the session does.

- F1: after a failed flush the session needs a rollback and every loaded
  attribute is expired; reading ``document.id`` before ``rollback()`` raised
  ``PendingRollbackError`` out of the outer ``except``.
- F2: ``Document.add_metadata`` mutated the JSON dict in place, so the
  session saw no change and a re-run's ``processing_results`` and
  ``processing_warnings`` were never written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import src.models  # noqa: F401  register every table on Base.metadata
from src.models.base import Base
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.processing import JobStatus, JobType, ProcessingJob
from src.services.processing import multimodal_processing_service as mps
from tests.unit.services.test_multimodal_entity_step import _ok

pytestmark = pytest.mark.unit

KG = "Knowledge Graph Storage"


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(engine, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(db: Session, metadata: dict[str, Any] | None = None) -> tuple[Any, Any]:
    org_id, doc_id = uuid4(), uuid4()
    doc = Document(
        id=doc_id,
        title="Doc",
        filename="doc.txt",
        file_path="local:///tmp/doc.txt",
        file_size_bytes=10,
        mime_type="text/plain",
        document_type=DocumentType.TEXT,
        organization_id=org_id,
        content_text="Ada Lovelace worked with Charles Babbage.",
        processing_status=ProcessingStatus.PROCESSING,
        document_metadata=metadata,
    )
    job = ProcessingJob(
        id=uuid4(),
        job_type=JobType.DOCUMENT_INGESTION,
        status=JobStatus.PENDING,
        organization_id=org_id,
        document_id=doc_id,
        parameters={"document_id": str(doc_id)},
        total_steps=5,
    )
    db.add_all([doc, job])
    db.commit()
    return doc, job


def _service(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **broken: Any
) -> mps.MultimodalProcessingService:
    svc = object.__new__(mps.MultimodalProcessingService)
    svc.db = db
    svc.entity_service = None
    svc.processed_dir = tmp_path
    steps = mps.MultimodalProcessingService.get_processing_pipeline(
        svc, DocumentType.TEXT
    )
    for step in steps:
        step.function = broken.get(step.name, _ok)
    monkeypatch.setattr(svc, "get_processing_pipeline", lambda _type: steps)
    monkeypatch.setattr(svc, "_stage_for_bedrock", lambda *_args: None)
    return svc


def _stored(db: Session, doc_id: Any, job_id: Any) -> tuple[Any, Any]:
    """The rows as committed, not as the session remembers them."""
    db.rollback()
    db.expire_all()
    return db.get(Document, doc_id), db.get(ProcessingJob, job_id)


@pytest.mark.asyncio
async def test_a_failed_flush_still_ends_the_run_failed(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc, job = _seed(db)
    doc_id, job_id = doc.id, job.id

    async def corrupting(document: Any, _job: Any) -> dict:
        document.filename = None  # NOT NULL: the next flush fails
        return {}

    svc = _service(db, monkeypatch, tmp_path, **{"Text Extraction": corrupting})

    results = await svc.process_document(doc, job)

    assert results["success"] is False
    stored_doc, stored_job = _stored(db, doc_id, job_id)
    assert stored_doc.processing_status == ProcessingStatus.FAILED
    assert stored_doc.processing_error == "Document processing failed"
    assert stored_doc.is_indexed is False
    assert stored_job.status == JobStatus.FAILED


@pytest.mark.asyncio
async def test_a_rerun_replaces_the_stored_results_and_records_warnings(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc, job = _seed(db, {"processing_results": {"stale": True}, "keep": 1})
    doc_id, job_id = doc.id, job.id

    async def kg_down(_document: Any, _job: Any) -> dict:
        raise mps.KnowledgeGraphStorageFailed(mps._KG_STORAGE_ERROR)

    svc = _service(db, monkeypatch, tmp_path, **{KG: kg_down})

    results = await svc.process_document(doc, job)

    assert results["success"] is True
    stored_doc, _job = _stored(db, doc_id, job_id)
    metadata = stored_doc.document_metadata
    assert "stale" not in metadata["processing_results"]
    assert metadata["processing_results"]["warnings"] == [f"{KG} failed"]
    assert metadata["processing_warnings"] == [f"{KG} failed"]
    assert metadata["keep"] == 1
