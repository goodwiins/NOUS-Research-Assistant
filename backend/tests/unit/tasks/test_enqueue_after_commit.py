"""Contract for ``enqueue_after_commit`` (PR 1, Task 1.2).

The helper closes the worker-reads-before-commit race: a Celery enqueue is
deferred to fire *after* the owning ``AsyncSession`` commits, and is dropped if
the session rolls back. Pinned behaviors:

- fires ``task.delay(*args, **kwargs)`` only after a successful commit;
- dropped on rollback (never fires), and a later commit can't resurrect it;
- a raising ``delay`` is logged (``enqueue_after_commit_failed``) without
  corrupting the session or blocking sibling registrations;
- multiple registrations fire in registration order;
- a second commit does not re-fire an already-fired registration.

Uses an in-memory aiosqlite async session (mirrors the pattern in
``test_agent_run_tasks``) and a stub task (``name`` + ``delay`` mock) — no real
Celery broker.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import AsyncIterator
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.tasks.enqueue import enqueue_after_commit, enqueue_after_commit_apply_async

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _task(name: str = "my.task") -> SimpleNamespace:
    return SimpleNamespace(name=name, delay=MagicMock())


async def _work(db: AsyncSession) -> None:
    # Open a real transaction so rollback emits after_rollback (an empty
    # rollback is a no-op that fires nothing).
    await db.execute(text("SELECT 1"))


async def test_fires_only_after_commit(db: AsyncSession) -> None:
    task = _task()
    enqueue_after_commit(db, task, "doc-1", priority="high")

    # Registration alone must not enqueue.
    task.delay.assert_not_called()

    await _work(db)
    await db.commit()

    task.delay.assert_called_once_with("doc-1", priority="high")


async def test_dropped_on_rollback(db: AsyncSession) -> None:
    task = _task()
    enqueue_after_commit(db, task)

    await _work(db)
    await db.rollback()
    task.delay.assert_not_called()

    # A later commit must not resurrect the dropped registration.
    await _work(db)
    await db.commit()
    task.delay.assert_not_called()


async def test_multiple_fire_in_registration_order(db: AsyncSession) -> None:
    order: list[str] = []
    tasks = []
    for name in ("t1", "t2", "t3"):
        t = SimpleNamespace(
            name=name,
            delay=MagicMock(side_effect=lambda *a, _n=name, **k: order.append(_n)),
        )
        tasks.append(t)
        enqueue_after_commit(db, t)

    await _work(db)
    await db.commit()

    assert order == ["t1", "t2", "t3"]


async def test_raising_task_logs_without_corrupting_session(db: AsyncSession) -> None:
    boom = SimpleNamespace(
        name="boom.task", delay=MagicMock(side_effect=RuntimeError("broker down"))
    )
    after = _task("after.task")
    enqueue_after_commit(db, boom)
    enqueue_after_commit(db, after)

    with patch("src.tasks.enqueue.logger") as mock_logger:
        await _work(db)
        await db.commit()  # must NOT raise despite boom.delay raising

    mock_logger.exception.assert_called_once_with(
        "enqueue_after_commit_failed", task="boom.task"
    )
    # A raising registration must not block the ones after it.
    after.delay.assert_called_once_with()

    # Session is still usable after the swallowed error.
    await _work(db)
    await db.commit()


async def test_apply_async_variant_fires_with_options_after_commit(
    db: AsyncSession,
) -> None:
    task = SimpleNamespace(name="kg.task", apply_async=MagicMock())
    enqueue_after_commit_apply_async(db, task, args=["kg-1"], queue="entity_processing")

    # Registration alone must not enqueue.
    task.apply_async.assert_not_called()

    await _work(db)
    await db.commit()

    task.apply_async.assert_called_once_with(
        args=["kg-1"], kwargs=None, queue="entity_processing"
    )


async def test_apply_async_variant_dropped_on_rollback(db: AsyncSession) -> None:
    task = SimpleNamespace(name="kg.task", apply_async=MagicMock())
    enqueue_after_commit_apply_async(db, task, args=["kg-1"], queue="q")

    await _work(db)
    await db.rollback()
    task.apply_async.assert_not_called()


async def test_second_commit_does_not_refire(db: AsyncSession) -> None:
    first = _task("first")
    enqueue_after_commit(db, first)

    await _work(db)
    await db.commit()
    first.delay.assert_called_once()

    # Commit again with no new registration — must not re-fire.
    await _work(db)
    await db.commit()
    first.delay.assert_called_once()

    # A fresh registration fires on the next commit; the earlier one stays put.
    second = _task("second")
    enqueue_after_commit(db, second)
    await _work(db)
    await db.commit()
    second.delay.assert_called_once()
    first.delay.assert_called_once()


# --- GOO-463: the dispatched task id must be recordable after commit ---------
#
# The lost-job reconciler treats PENDING/celery_task_id=NULL as "never
# dispatched". Producers therefore need the id of the message the post-commit
# dispatch actually published, so they can persist it once it landed.


async def test_handle_exposes_task_id_only_after_commit(db: AsyncSession) -> None:
    task = SimpleNamespace(
        name="my.task", delay=MagicMock(return_value=SimpleNamespace(id="tid-1"))
    )
    handle = enqueue_after_commit(db, task, "doc-1")

    assert handle.task_id is None  # nothing published yet

    await _work(db)
    await db.commit()

    assert handle.task_id == "tid-1"


async def test_apply_async_handle_exposes_task_id(db: AsyncSession) -> None:
    task = SimpleNamespace(
        name="kg.task",
        apply_async=MagicMock(return_value=SimpleNamespace(id="tid-kg")),
    )
    handle = enqueue_after_commit_apply_async(db, task, args=["kg-1"], queue="q")

    await _work(db)
    await db.commit()

    assert handle.task_id == "tid-kg"


async def test_handle_task_id_none_when_dispatch_raises_or_rolls_back(
    db: AsyncSession,
) -> None:
    boom = SimpleNamespace(
        name="boom.task", delay=MagicMock(side_effect=RuntimeError("broker down"))
    )
    raised = enqueue_after_commit(db, boom)
    await _work(db)
    await db.commit()
    assert raised.task_id is None

    dropped = enqueue_after_commit(db, _task("dropped.task"))
    await _work(db)
    await db.rollback()
    assert dropped.task_id is None


@pytest_asyncio.fixture
async def job_db() -> AsyncIterator[AsyncSession]:
    from src.models.processing import ProcessingJob

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ProcessingJob.__table__.create)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed_job(db: AsyncSession, *, status, celery_task_id=None):
    import uuid

    from src.models.processing import JobType, ProcessingJob

    job = ProcessingJob(
        id=uuid.uuid4(),
        job_type=JobType.DOCUMENT_INGESTION,
        status=status,
        organization_id=uuid.uuid4(),
        celery_task_id=celery_task_id,
        queue_name="document_processing",
    )
    db.add(job)
    await db.commit()
    return job.id


async def test_record_dispatched_task_id_stamps_never_started_pending_job(
    job_db: AsyncSession,
) -> None:
    from src.models.processing import JobStatus, ProcessingJob
    from src.tasks.enqueue import record_dispatched_task_id

    job_id = await _seed_job(job_db, status=JobStatus.PENDING)

    await record_dispatched_task_id(job_db, job_id, "tid-1")

    job_db.expire_all()
    job = await job_db.get(ProcessingJob, job_id)
    assert job.celery_task_id == "tid-1"
    assert job.status is JobStatus.PENDING


async def test_record_dispatched_task_id_never_overwrites_a_worker_claim(
    job_db: AsyncSession,
) -> None:
    from src.models.processing import JobStatus, ProcessingJob
    from src.tasks.enqueue import record_dispatched_task_id

    running = await _seed_job(
        job_db, status=JobStatus.RUNNING, celery_task_id="worker-tid"
    )
    stamped = await _seed_job(
        job_db, status=JobStatus.PENDING, celery_task_id="earlier-tid"
    )

    await record_dispatched_task_id(job_db, running, "late-tid")
    await record_dispatched_task_id(job_db, stamped, "late-tid")

    job_db.expire_all()
    assert (await job_db.get(ProcessingJob, running)).celery_task_id == "worker-tid"
    assert (await job_db.get(ProcessingJob, stamped)).celery_task_id == "earlier-tid"


async def test_record_dispatched_task_id_without_id_is_a_noop(
    job_db: AsyncSession,
) -> None:
    from src.models.processing import JobStatus, ProcessingJob
    from src.tasks.enqueue import record_dispatched_task_id

    job_id = await _seed_job(job_db, status=JobStatus.PENDING)

    await record_dispatched_task_id(job_db, job_id, None)

    job_db.expire_all()
    assert (await job_db.get(ProcessingJob, job_id)).celery_task_id is None


async def test_record_dispatched_task_id_swallows_db_errors(
    job_db: AsyncSession,
) -> None:
    from src.tasks.enqueue import record_dispatched_task_id

    with patch.object(job_db, "execute", side_effect=RuntimeError("db gone")):
        # Must not raise: the message is already published, so a failed stamp
        # degrades to the pre-GOO-463 behavior instead of failing the request.
        await record_dispatched_task_id(job_db, "job-1", "tid-1")
