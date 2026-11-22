"""Evaluation task failure paths (GOO-684, review of #2224).

* ``on_failure`` must keep a job the task already finished: the task's own
  handler records the real outcome first, so overwriting it would replace a
  COMPLETED job or a specific failure message with a generic one.
* Real-time evaluation created its job PENDING and never started it, so the
  worker-lost branch (RUNNING + same task id) could never match and a dead
  attempt left a job PENDING forever.
* Batch evaluation ran every query before saving anything, so a soft time
  limit lost all finished metrics while the job message claimed they were
  kept; a query that errored also shifted every later metric onto the wrong
  query text.
* A skipped redelivery reported Celery SUCCESS; it is now recorded as
  SKIPPED and the message is acked without running anything.

Mutation checks for these guards are recorded in
``docs/testing/evaluation-task-mutation-checks.md``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from celery.exceptions import Ignore, SoftTimeLimitExceeded
from pydantic import ValidationError

from src.core.config import Settings
from src.models.evaluation import EvaluationMetric, EvaluationStatus
from src.services.evaluation import rag_evaluation_service as svc_mod
from src.tasks import evaluation_tasks as et

pytestmark = pytest.mark.unit

FAILED = EvaluationStatus.FAILED.value
RUNNING = EvaluationStatus.RUNNING.value


def _job(status: str) -> MagicMock:
    job = MagicMock()
    job.status = status
    job.error_message = None
    job.parameters = {}
    job.organization_id = "org"
    job.user_id = "user"

    def fail_job(message: str) -> None:
        job.status = FAILED
        job.error_message = message

    job.fail_job.side_effect = fail_job
    return job


def _db(job: Any) -> MagicMock:
    db = MagicMock()
    chain = db.query.return_value.filter.return_value
    chain.first.return_value = job
    chain.with_for_update.return_value.first.return_value = job
    return db


# ---- on_failure keeps a finished job ----------------------------------------


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_on_failure_leaves_a_terminal_job_alone(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    job = _job(status)
    job.error_message = "No items were successfully processed"
    db = _db(job)
    monkeypatch.setattr(et, "SessionLocal", lambda: db)

    et.run_rag_triad_evaluation.on_failure(
        RuntimeError("late error"), "task-A", ("job-1",), {}, None
    )

    job.fail_job.assert_not_called()
    assert job.status == status
    assert job.error_message == "No items were successfully processed"
    db.commit.assert_not_called()


@pytest.mark.parametrize("status", ["pending", "running"])
def test_on_failure_fails_an_unfinished_job(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    job = _job(status)
    db = _db(job)
    monkeypatch.setattr(et, "SessionLocal", lambda: db)

    et.run_rag_triad_evaluation.on_failure(
        SoftTimeLimitExceeded(), "task-A", ("job-1",), {}, None
    )

    assert job.status == FAILED
    assert "time limit" in job.error_message
    db.commit.assert_called_once()


# ---- real-time evaluation starts its job ------------------------------------


def test_real_time_job_is_running_while_it_evaluates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(None)  # no earlier delivery of this task id
    added: list[Any] = []
    db.add.side_effect = added.append
    monkeypatch.setattr(et, "SessionLocal", lambda: db)
    seen: list[Any] = []

    async def triad(*_: Any, **__: Any) -> Any:
        [job] = added
        seen.append((job.status, job.started_at))
        return MagicMock(overall_score=0.9)

    monkeypatch.setattr(et.rag_evaluation_service, "run_rag_triad_evaluation", triad)

    result = et.run_real_time_evaluation.apply(
        args=("q", "a", ["ctx"]), task_id="task-A"
    )

    assert result.result["status"] == "completed"
    [(status, started_at)] = seen
    assert status == RUNNING and started_at is not None


@pytest.mark.parametrize("status", ["running", "pending"])
def test_real_time_redelivery_fails_the_unfinished_job(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    # "pending" covers rows written before the job was started on creation.
    existing = _job(status)
    existing.id = "job-3"
    db = _db(existing)
    monkeypatch.setattr(et, "SessionLocal", lambda: db)
    triad = AsyncMock()
    monkeypatch.setattr(et.rag_evaluation_service, "run_rag_triad_evaluation", triad)

    result = et.run_real_time_evaluation.apply(
        args=("q", "a", ["ctx"]), task_id="task-A"
    )

    assert result.state == "IGNORED"
    assert existing.status == FAILED
    assert "not restarted" in existing.error_message
    triad.assert_not_called()
    db.add.assert_not_called()


# ---- batch evaluation saves as it goes --------------------------------------


def _metrics(score: float) -> SimpleNamespace:
    return SimpleNamespace(
        overall_score=score,
        answer_relevancy=score,
        faithfulness=score,
        contextual_relevancy=score,
        hallucination_rate=0.0,
        response_time_ms=10.0,
        metadata={"generated_answer": "a", "retrieved_context": ["c"]},
    )


def _batch_db(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, list[str]]:
    """A job db that records adds and commits in order."""
    job = _job("pending")
    db = _db(job)
    events: list[str] = []
    db.add.side_effect = lambda obj: events.append(
        f"add:{obj.query}" if isinstance(obj, EvaluationMetric) else "add"
    )
    db.commit.side_effect = lambda: events.append("commit")
    db.rollback.side_effect = lambda: events.append("rollback")
    db.job = job
    monkeypatch.setattr(et, "SessionLocal", lambda: db)
    return db, events


def test_batch_soft_limit_keeps_the_metrics_already_evaluated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, events = _batch_db(monkeypatch)
    pipeline = AsyncMock(side_effect=[[_metrics(0.9)], SoftTimeLimitExceeded()])
    monkeypatch.setattr(et.rag_evaluation_service, "evaluate_search_pipeline", pipeline)

    result = et.run_batch_evaluation.apply(
        args=("job-2", ["q1", "q2", "q3"]), task_id="task-A"
    )

    assert isinstance(result.result, SoftTimeLimitExceeded)
    assert pipeline.await_count == 2  # q3 never ran
    # q1's metric was committed before the limit hit, so the rollback keeps it.
    assert events.index("add:q1") < events.index("commit", events.index("add:q1"))
    assert events.index("commit", events.index("add:q1")) < events.index("rollback")
    assert db.job.status == FAILED
    assert "time limit" in db.job.error_message
    db.job.complete_job.assert_not_called()


def test_batch_metric_keeps_its_own_query_when_an_earlier_one_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, events = _batch_db(monkeypatch)
    # The pipeline drops a query that errors, so q1 yields no metric.
    pipeline = AsyncMock(side_effect=[[], [_metrics(0.8)]])
    monkeypatch.setattr(et.rag_evaluation_service, "evaluate_search_pipeline", pipeline)

    result = et.run_batch_evaluation.apply(
        args=("job-2", ["q1", "q2"]), task_id="task-A"
    ).result

    assert [e for e in events if e.startswith("add:")] == ["add:q2"]
    assert result["processed_queries"] == 1
    assert [c.args[0] for c in pipeline.await_args_list] == [["q1"], ["q2"]]
    db.job.update_progress.assert_called_with(2)


async def test_search_pipeline_does_not_swallow_the_soft_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = MagicMock(side_effect=SoftTimeLimitExceeded())
    monkeypatch.setattr(svc_mod.hybrid_search_service, "search", search)

    with pytest.raises(SoftTimeLimitExceeded):
        await svc_mod.rag_evaluation_service.evaluate_search_pipeline(
            ["q1", "q2"], "org", "user"
        )
    assert search.call_count == 1


# ---- skipped deliveries are not SUCCESS -------------------------------------


def test_terminal_redelivery_is_ignored_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job("completed")
    monkeypatch.setattr(et, "SessionLocal", lambda: _db(job))

    result = et.run_batch_evaluation.apply(args=("job-2", ["q1"]), task_id="task-A")

    assert result.state == "IGNORED"
    job.start_job.assert_not_called()


def test_skip_records_a_skipped_state_on_a_real_worker() -> None:
    task = MagicMock()
    task.request.is_eager = False
    response = {"status": "completed", "job_id": "j", "skipped": "duplicate_delivery"}

    with pytest.raises(Ignore):
        et._skip_delivery(task, response)

    task.update_state.assert_called_once_with(state="SKIPPED", meta=response)


def test_skip_state_failure_still_acks_the_delivery() -> None:
    task = MagicMock()
    task.request.is_eager = False
    task.update_state.side_effect = ConnectionError("result backend down")

    with pytest.raises(Ignore):
        et._skip_delivery(task, {"skipped": "duplicate_delivery"})


# ---- time limit headroom ----------------------------------------------------


def test_hard_limit_leaves_headroom_under_the_visibility_timeout() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"EVALUATION_TASK_TIME_LIMIT_SECONDS": 3599})
