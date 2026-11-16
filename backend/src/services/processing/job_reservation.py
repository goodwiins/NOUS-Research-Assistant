"""Reserve-then-publish persistence for producers on the sync session.

GOO-464: a producer must make a job durable before its Celery message can
reach a worker. The sync-session twin of
``ProcessingService.queue_processing_job``, split so each function owns exactly
one transaction and the caller (a router) keeps the broker call and the HTTP
error mapping:

1. ``reserve_job`` — add the job, stamp a fresh task id, mark it QUEUED, commit.
2. The caller publishes under that task id.
3. If publication raises, ``fail_unpublished_job`` fails the reserved attempt.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from src.models.processing import JobStatus, ProcessingJob


def reserve_job(db: Session, job: ProcessingJob, queue: str) -> str:
    """Persist ``job`` as a QUEUED attempt under a new task id; return the id.

    Commits. Publish the task with ``task_id=<returned id>`` only after this
    returns.
    """
    task_id = str(uuid4())
    db.add(job)
    db.flush()
    # Legacy Column-declared model: the instance attribute holds a plain str.
    job.celery_task_id = task_id  # type: ignore[assignment]
    job.queue_job(queue_name=queue)
    db.commit()
    return task_id


def fail_unpublished_job(db: Session, job: ProcessingJob, task_id: str) -> bool:
    """Fail the attempt reserved under ``task_id`` after its publish raised.

    A broker can accept a message and still raise, so a worker may already own
    the job. Re-read the row under a lock and fail it only while it is still
    this QUEUED attempt. Commits either way; returns whether it failed the job.
    """
    db.refresh(job, with_for_update=True)
    failed = bool(job.status == JobStatus.QUEUED) and bool(
        job.celery_task_id == task_id
    )
    if failed:
        job.fail_job("Failed to queue job: broker unavailable")
    db.commit()
    return failed
