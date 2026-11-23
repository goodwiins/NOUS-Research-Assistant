"""GOO-776: the multimodal pipeline's late-crash and fail_job paths, against a
real SQLAlchemy session (SQLite), as GOO-743's tests are.

- A crash after ``success`` is computed returned ``success=True`` with an
  error, so the retry task reported ``completed`` while the rows said FAILED.
- A ``fail_job`` that raised inside an error path escaped and hid the
  original error.
- The retry task read its status from the returned dict, not from the job
  row the run committed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from src.models.document import ProcessingStatus
from src.models.processing import JobStatus, ProcessingJob
from src.tasks import document_processing_tasks as tasks
from tests.unit.services.test_multimodal_real_session import (  # noqa: F401
    _seed,
    _service,
    _stored,
    db,
)

pytestmark = pytest.mark.unit

SVC_LOGGER = "src.services.processing.multimodal_processing_service"


class OriginalError(RuntimeError):
    pass


def _late_crash(svc: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Crash after the pipeline has computed ``success``."""

    async def crash(*_args: Any) -> None:
        raise OriginalError("late crash")

    monkeypatch.setattr(svc, "update_document_with_results", crash)


def _broken_fail_job(job: ProcessingJob, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_job(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("fail_job is broken")

    monkeypatch.setattr(job, "fail_job", fail_job)


def _fail_job_logged(caplog: pytest.LogCaptureFixture) -> bool:
    return any(
        r.name == SVC_LOGGER
        and r.exc_info is not None
        and isinstance(r.exc_info[1], ValueError)
        and "fail_job" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_a_crash_after_success_is_computed_reports_failure(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path  # noqa: F811
) -> None:
    doc, job = _seed(db)
    doc_id, job_id = doc.id, job.id
    svc = _service(db, monkeypatch, tmp_path)
    _late_crash(svc, monkeypatch)

    results = await svc.process_document(doc, job)

    assert results["success"] is False
    assert results["errors"] == ["Document processing failed"]
    stored_doc, stored_job = _stored(db, doc_id, job_id)
    assert stored_doc.processing_status == ProcessingStatus.FAILED
    assert stored_job.status == JobStatus.FAILED


@pytest.mark.asyncio
async def test_a_failing_fail_job_on_the_crash_path_surfaces_the_original(
    db: Session,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    doc, job = _seed(db)
    doc_id, job_id = doc.id, job.id
    svc = _service(db, monkeypatch, tmp_path)
    _late_crash(svc, monkeypatch)
    _broken_fail_job(job, monkeypatch)

    with caplog.at_level(logging.ERROR, logger=SVC_LOGGER):
        with pytest.raises(OriginalError, match="late crash"):
            await svc.process_document(doc, job)

    assert _fail_job_logged(caplog)
    # The document still ends terminal: a PROCESSING row is never retried.
    stored_doc, _job = _stored(db, doc_id, job_id)
    assert stored_doc.processing_status == ProcessingStatus.FAILED


@pytest.mark.asyncio
async def test_a_failing_fail_job_on_a_required_step_surfaces_the_step_error(
    db: Session,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    doc, job = _seed(db)
    doc_id, job_id = doc.id, job.id

    async def step_down(_document: Any, _job: Any) -> dict:
        raise OriginalError("required step down")

    svc = _service(db, monkeypatch, tmp_path, **{"Text Extraction": step_down})
    _broken_fail_job(job, monkeypatch)

    with caplog.at_level(logging.ERROR, logger=SVC_LOGGER):
        with pytest.raises(OriginalError, match="required step down"):
            await svc.process_document(doc, job)

    assert _fail_job_logged(caplog)
    stored_doc, _job = _stored(db, doc_id, job_id)
    assert stored_doc.processing_status == ProcessingStatus.FAILED


def _run_retry_task(
    db: Session, monkeypatch: pytest.MonkeyPatch, outcome: dict[str, Any], end: str
) -> dict[str, Any]:
    """Run retry_failed_processing on ``db``; the run returns ``outcome`` and
    commits the job as ``end`` ("complete" or "fail")."""
    doc, job = _seed(db)
    job.status = JobStatus.FAILED
    db.commit()

    class FakeService:
        def __init__(self, session: Session) -> None:
            self.db = session

        async def process_document(self, _document: Any, run_job: Any) -> dict:
            if end == "complete":
                run_job.complete_job(result={})
            else:
                run_job.fail_job("Processing step 'Text Extraction' failed")
            self.db.commit()
            return outcome

    monkeypatch.setattr(tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(tasks, "MultimodalProcessingService", FakeService)
    return tasks.retry_failed_processing.run(str(job.id))


def test_the_retry_task_reports_failed_when_the_job_row_failed(
    db: Session, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    result = _run_retry_task(
        db, monkeypatch, {"success": True, "errors": []}, end="fail"
    )

    assert result["status"] == "failed"


def test_the_retry_task_reports_completed_when_the_job_row_completed(
    db: Session, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    result = _run_retry_task(
        db, monkeypatch, {"success": True, "errors": []}, end="complete"
    )

    assert result["status"] == "completed"
