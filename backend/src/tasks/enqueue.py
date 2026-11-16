"""Post-commit Celery enqueue for async SQLAlchemy sessions.

``enqueue_after_commit`` (``.delay``) and ``enqueue_after_commit_apply_async``
(``.apply_async``, for callers that need a queue/options override) defer a task
enqueue until the owning session commits, closing the worker-reads-before-commit
race without reintroducing the orphan-row problem the raw ``flush -> enqueue ->
commit`` ordering was built to avoid: the enqueue fires *after* the row is
durable, and is dropped entirely if the session rolls back.

Mechanics: pending enqueues accumulate on the session (``sync_session.info``) as
zero-arg thunks, and a single pair of listeners per session drains them on
``after_commit`` and discards them on ``after_rollback``. Draining before firing
means a raising enqueue can't block its siblings and a second commit can't
re-fire an already-fired registration.

Each registration returns an ``EnqueueHandle`` whose ``task_id`` is the id of
the published message once the post-commit dispatch succeeded (``None`` before
commit, after a rollback, or when the dispatch raised). Producers of
``ProcessingJob`` rows pass it to ``record_dispatched_task_id`` so the lost-job
reconciler can tell a message queued behind a backlog from one that never
reached the broker (GOO-463).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import structlog
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)

# Key under which the per-session list of pending enqueues lives.
_PENDING_KEY = "_enqueue_after_commit_pending"

# Per-session queue of deferred enqueues: (task, thunk). ``task`` is retained
# only so a raising thunk can be logged with the task name.
_Pending = list[tuple[Any, Callable[[], Any]]]


@dataclass
class EnqueueHandle:
    """Result slot for one deferred enqueue; filled when the dispatch fires."""

    result: Any = None

    @property
    def task_id(self) -> str | None:
        """The published message's id, or None if nothing was published."""
        task_id = getattr(self.result, "id", None)
        return task_id if isinstance(task_id, str) and task_id else None


def enqueue_after_commit(
    db: AsyncSession, task: Any, *args: Any, **kwargs: Any
) -> EnqueueHandle:
    """Register ``task.delay(*args, **kwargs)`` to fire after ``db`` commits.

    Dropped (never fired) if the session rolls back instead. Registrations fire
    in order on the next successful commit; a raising ``delay`` is logged, not
    propagated.
    """
    return _register(db, task, lambda: task.delay(*args, **kwargs))


def enqueue_after_commit_apply_async(
    db: AsyncSession,
    task: Any,
    *,
    args: list[Any] | tuple[Any, ...] | None = None,
    kwargs: dict[str, Any] | None = None,
    **options: Any,
) -> EnqueueHandle:
    """Register ``task.apply_async(args, kwargs, **options)`` for after commit.

    The ``.apply_async`` sibling of ``enqueue_after_commit``, for callers that
    need a ``queue=`` (or other apply_async option) override. Same post-commit /
    drop-on-rollback semantics.
    """
    return _register(
        db, task, lambda: task.apply_async(args=args, kwargs=kwargs, **options)
    )


async def record_dispatched_task_id(
    db: AsyncSession, job_id: Any, task_id: str | None
) -> None:
    """Persist ``task_id`` on a ``ProcessingJob`` whose message was published.

    Call after the commit that fired the dispatch. The lost-job reconciler
    re-enqueues PENDING rows with ``celery_task_id IS NULL`` as "never
    dispatched"; without this stamp a message merely queued behind a backlog
    looks lost and is published again (GOO-463).

    Only a still-PENDING, never-stamped row is updated, so a worker that already
    claimed the job (``start_job`` stamps its own id) is never overwritten.
    No-op without a task id. Best effort: the message is already in the broker,
    so a failed stamp is logged rather than failing the caller — that row just
    keeps the pre-fix reconciler behaviour.

    The stamp runs on its own short-lived session bound to ``db``'s engine, not
    on ``db`` itself: the producer still builds its response from objects in
    ``db``, and rolling ``db`` back after a failed stamp would expire them, so
    the next attribute read would be an implicit async lazy load.
    """
    if not isinstance(task_id, str) or not task_id:
        return
    from sqlalchemy import update

    from src.models.processing import JobStatus, ProcessingJob

    try:
        async with AsyncSession(db.bind) as stamp_db:
            await stamp_db.execute(
                update(ProcessingJob)
                .where(
                    ProcessingJob.id == job_id,
                    ProcessingJob.status == JobStatus.PENDING,
                    ProcessingJob.celery_task_id.is_(None),
                )
                .values(celery_task_id=task_id)
                .execution_options(synchronize_session=False)
            )
            await stamp_db.commit()
    except Exception:
        # Leaving the ``async with`` closed (and so rolled back) stamp_db only.
        logger.warning(
            "record_dispatched_task_id_failed",
            job_id=str(job_id),
            task_id=task_id,
            exc_info=True,
        )


def _register(db: AsyncSession, task: Any, thunk: Callable[[], Any]) -> EnqueueHandle:
    handle = EnqueueHandle()

    def _fire_and_capture() -> None:
        handle.result = thunk()

    sync_session = db.sync_session
    pending: _Pending | None = sync_session.info.get(_PENDING_KEY)
    if pending is None:
        pending = []
        sync_session.info[_PENDING_KEY] = pending
        _install_listeners(sync_session)
    pending.append((task, _fire_and_capture))
    return handle


def _install_listeners(sync_session: Session) -> None:
    @event.listens_for(sync_session, "after_commit")
    def _fire(session: Session) -> None:
        pending: _Pending | None = session.info.get(_PENDING_KEY)
        if not pending:
            return
        # Drain first: a raising enqueue must not block siblings, and a later
        # commit must not re-fire these.
        session.info[_PENDING_KEY] = []
        for task, thunk in pending:
            try:
                thunk()
            except Exception:
                logger.exception("enqueue_after_commit_failed", task=task.name)

    @event.listens_for(sync_session, "after_rollback")
    def _discard(session: Session) -> None:
        session.info[_PENDING_KEY] = []
