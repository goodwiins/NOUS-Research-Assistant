"""Reserve-then-publish persistence for sync-session job producers (GOO-464).

``reserve_job`` makes the attempt durable (QUEUED + a fresh task id, one
commit) before anything is published; ``fail_unpublished_job`` fails that
attempt after a publish error (one commit), but only while the row is still
the attempt this producer reserved, never a worker's newer state.
"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.models.processing import JobStatus, JobType, ProcessingJob
from src.services.processing.job_reservation import fail_unpublished_job, reserve_job

pytestmark = pytest.mark.unit


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite:///:memory:")
    ProcessingJob.__table__.create(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _new_job() -> ProcessingJob:
    return ProcessingJob(
        job_type=JobType.ENTITY_EXTRACTION,
        status=JobStatus.PENDING,
        organization_id=uuid.uuid4(),
        queue_name="entity_processing",
    )


def test_reserve_job_commits_a_queued_attempt_with_a_fresh_task_id(
    factory: sessionmaker[Session],
) -> None:
    with factory() as db:
        job = _new_job()
        task_id = reserve_job(db, job, "entity_processing")

    assert task_id
    with factory() as check:
        stored = check.get(ProcessingJob, job.id)
        assert stored is not None  # committed, visible to another session
        assert stored.status is JobStatus.QUEUED
        assert stored.celery_task_id == task_id
        assert stored.queue_name == "entity_processing"


def test_fail_unpublished_job_fails_the_reserved_attempt(
    factory: sessionmaker[Session],
) -> None:
    with factory() as db:
        job = _new_job()
        task_id = reserve_job(db, job, "entity_processing")
        assert fail_unpublished_job(db, job, task_id) is True

    with factory() as check:
        stored = check.get(ProcessingJob, job.id)
        assert stored is not None
        assert stored.status is JobStatus.FAILED


def test_fail_unpublished_job_leaves_a_worker_claim_alone(
    factory: sessionmaker[Session],
) -> None:
    with factory() as db:
        job = _new_job()
        task_id = reserve_job(db, job, "entity_processing")
        # The broker accepted the message despite raising; a worker started.
        with factory() as worker_db:
            claimed = worker_db.get(ProcessingJob, job.id)
            assert claimed is not None
            claimed.start_job(worker_id=task_id, celery_task_id=task_id)
            worker_db.commit()

        assert fail_unpublished_job(db, job, task_id) is False

    with factory() as check:
        stored = check.get(ProcessingJob, job.id)
        assert stored is not None
        assert stored.status is JobStatus.RUNNING
