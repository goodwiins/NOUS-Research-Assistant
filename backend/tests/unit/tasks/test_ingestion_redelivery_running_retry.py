"""A redelivered ingestion message that meets a *young* RUNNING row must retry,
not ack-and-drop (GOO-462, audit 2026-11-13 finding 5).

``celery_app`` sets ``task_acks_late=True`` + ``task_reject_on_worker_lost``, so
when a worker dies mid-ingestion the broker redelivers the message. Before the
fix ``process_document_ingestion`` answered a ``"running"`` claim with a returned
skip, which acked the message: the dead worker's RUNNING row then sat until the
sweeper failed it. The task now raises ``self.retry(countdown=...)`` so the same
message comes back once the row can be reclaimed (``reclaimed_stale``).

Mutation checks (``docs/engineering/testing.md``):

* ``test_running_claim_retries_instead_of_skipping`` fails on the pre-fix
  ``return {... "skipped": "running"}`` branch.
* ``test_retry_is_not_converted_into_job_failed`` fails when the
  ``except Retry: raise`` clause ahead of the broad ``except Exception`` in
  ``process_document_ingestion`` (src/tasks/processing_tasks.py) is removed:
  the broad handler then marks the job and document FAILED.

Focused command (from ``backend/``)::

    python -m pytest -q -p no:cacheprovider \
        tests/unit/tasks/test_ingestion_redelivery_running_retry.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from celery.exceptions import MaxRetriesExceededError, Retry

pytestmark = pytest.mark.unit

from src.models.document import Document, ProcessingStatus
from src.models.processing import JobStatus, ProcessingJob
from src.tasks import processing_tasks as pt
from src.tasks.replay_guard import (
    DEFAULT_STALE_RUNNING_SECONDS,
    REDELIVERY_GRACE_SECONDS,
    ClaimResult,
    redelivery_countdown_seconds,
)
from tests.unit.tasks.test_replay_idempotency import (  # noqa: F401
    _seed_document,
    _seed_job,
    session_factory,
)


class _RetryRecorder:
    """Stands in for ``Task.retry``.

    ``Task.apply()`` re-executes an eager Retry inline (``retval.sig.apply``),
    which would loop here, so the stub raises a signature-less ``Retry`` and
    records the countdown the task asked for.
    """

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.exc = exc

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        raise Retry("stubbed", when=kwargs.get("countdown"))


# The broker redelivers the *same* message, so the redelivered attempt carries
# the task id the dead worker's claim already wrote to the row. That is exactly
# the id ``require_active_ingestion`` treats as "still owns the job", which is
# why a Retry reaching the broad handler would be recorded as FAILED.
REDELIVERED_TASK_ID = "redelivered-task-id"


def _seed_running_job(Session: Any, *, started_ago: timedelta) -> tuple[UUID, UUID]:
    org_id, doc_id, job_id = uuid4(), uuid4(), uuid4()
    setup = Session()
    _seed_document(setup, doc_id=doc_id, org_id=org_id, content_text="x")
    job = _seed_job(
        setup,
        job_id=job_id,
        doc_id=doc_id,
        org_id=org_id,
        status=JobStatus.RUNNING,
        started_at=datetime.now(timezone.utc) - started_ago,
    )
    job.celery_task_id = REDELIVERED_TASK_ID
    job.worker_id = REDELIVERED_TASK_ID
    setup.commit()
    setup.close()
    return job_id, doc_id


def _redeliver(job_id: UUID) -> Any:
    return pt.process_document_ingestion.apply(
        args=(str(job_id),), task_id=REDELIVERED_TASK_ID
    )


def _forbid_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_db: Any) -> None:
        raise AssertionError("pipeline must not run while another attempt is live")

    monkeypatch.setattr(pt, "ProcessingPipeline", _boom)


def _state(Session: Any, job_id: UUID, doc_id: UUID) -> tuple[Any, Any, Any]:
    db = Session()
    try:
        job = db.query(ProcessingJob).filter(ProcessingJob.id == job_id).first()
        doc = db.query(Document).filter(Document.id == doc_id).first()
        return job.status, job.error_message, doc.processing_status
    finally:
        db.close()


# ---------------------------------------------------------------------------
# (1) a "running" claim retries with a sane countdown
# ---------------------------------------------------------------------------


def test_running_claim_retries_instead_of_skipping(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    Session = session_factory
    job_id, doc_id = _seed_running_job(Session, started_ago=timedelta(seconds=60))
    monkeypatch.setattr(pt, "SessionLocal", Session)
    _forbid_pipeline(monkeypatch)
    recorder = _RetryRecorder()
    monkeypatch.setattr(pt.process_document_ingestion, "retry", recorder)

    res = _redeliver(job_id)

    assert recorder.calls, "a 'running' claim was acked and dropped instead of retried"
    assert res.state == "RETRY"
    countdown = recorder.calls[0]["countdown"]
    # Row is ~60s old: come back once it is ~900s old, plus the grace margin.
    expected = DEFAULT_STALE_RUNNING_SECONDS - 60 + REDELIVERY_GRACE_SECONDS
    assert expected - 2 <= countdown <= expected + 2
    assert countdown <= DEFAULT_STALE_RUNNING_SECONDS + REDELIVERY_GRACE_SECONDS
    # The live attempt's row is untouched.
    assert _state(Session, job_id, doc_id)[0] == JobStatus.RUNNING


@pytest.mark.parametrize(
    "stale_in,expected",
    [
        (0.0, REDELIVERY_GRACE_SECONDS),
        (-30.0, REDELIVERY_GRACE_SECONDS),  # already stale: come straight back
        (12.2, 13 + REDELIVERY_GRACE_SECONDS),
        (10**9, DEFAULT_STALE_RUNNING_SECONDS + REDELIVERY_GRACE_SECONDS),  # bounded
        (None, DEFAULT_STALE_RUNNING_SECONDS + REDELIVERY_GRACE_SECONDS),
    ],
)
def test_redelivery_countdown_is_bounded(stale_in: float | None, expected: int) -> None:
    claim = ClaimResult(False, "running", stale_in_seconds=stale_in)
    assert redelivery_countdown_seconds(claim) == expected


# ---------------------------------------------------------------------------
# (2) the Retry must escape the broad ``except Exception``
# ---------------------------------------------------------------------------


def test_retry_is_not_converted_into_job_failed(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: ``except Retry: raise`` ahead of the broad handler in
    process_document_ingestion. Without it the handler writes FAILED onto a job
    that another worker may still be running."""
    Session = session_factory
    job_id, doc_id = _seed_running_job(Session, started_ago=timedelta(seconds=60))
    monkeypatch.setattr(pt, "SessionLocal", Session)
    _forbid_pipeline(monkeypatch)
    monkeypatch.setattr(pt.process_document_ingestion, "retry", _RetryRecorder())

    _redeliver(job_id)

    status, error, doc_status = _state(Session, job_id, doc_id)
    assert status == JobStatus.RUNNING, f"Retry was turned into {status} ({error})"
    assert error is None
    assert doc_status == ProcessingStatus.PROCESSING


def test_exhausted_retry_budget_falls_back_to_skip(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the retry budget is spent the task degrades to the old ack-and-skip
    rather than marking a possibly-live job FAILED."""
    Session = session_factory
    job_id, doc_id = _seed_running_job(Session, started_ago=timedelta(seconds=60))
    monkeypatch.setattr(pt, "SessionLocal", Session)
    _forbid_pipeline(monkeypatch)
    monkeypatch.setattr(
        pt.process_document_ingestion,
        "retry",
        _RetryRecorder(exc=MaxRetriesExceededError("spent")),
    )

    result = _redeliver(job_id).get()

    assert result["skipped"] == "running"
    assert _state(Session, job_id, doc_id)[0] == JobStatus.RUNNING


# ---------------------------------------------------------------------------
# (3) terminal / deleted / missing still skip (no retry)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["terminal", "deleted", "missing"])
def test_non_running_skips_do_not_retry(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    Session = session_factory
    org_id, doc_id, job_id = uuid4(), uuid4(), uuid4()
    setup = Session()
    doc = _seed_document(setup, doc_id=doc_id, org_id=org_id, content_text="x")
    status = JobStatus.COMPLETED if scenario == "terminal" else JobStatus.QUEUED
    job = _seed_job(setup, job_id=job_id, doc_id=doc_id, org_id=org_id, status=status)
    if scenario == "deleted":
        doc.soft_delete()
    setup.commit()
    setup.close()

    monkeypatch.setattr(pt, "SessionLocal", Session)
    _forbid_pipeline(monkeypatch)
    recorder = _RetryRecorder()
    monkeypatch.setattr(pt.process_document_ingestion, "retry", recorder)

    if scenario == "missing":
        # The job row vanishes between the task's initial load and the locked
        # claim re-read: the claim answers "missing".
        from src.tasks import replay_guard

        monkeypatch.setattr(
            pt,
            "claim_job_for_processing",
            lambda *a, **k: replay_guard.ClaimResult(False, "missing"),
        )

    result = pt.process_document_ingestion.apply(args=(str(job_id),)).get()

    assert result["skipped"] == scenario
    assert recorder.calls == []
