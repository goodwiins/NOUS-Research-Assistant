"""Evaluation tasks: worker-lost redelivery and time limits (GOO-675).

``task_acks_late`` + ``task_reject_on_worker_lost`` redeliver the SAME message
(same task id) when a worker dies mid-run (OOM, SIGKILL, eviction). The job is
then RUNNING, and the old guard only skipped terminal jobs, so the redelivery
re-ran every item, paid LLM calls included, and duplicated the metrics. The
claim now stamps the owning task id; a RUNNING job claimed by this delivery is
marked FAILED instead of started over, and a RUNNING job owned by another
delivery is left alone.

#2208 made the soft limit stop the job, so the inherited global 300s limit
failed every evaluation longer than five minutes; the tasks now carry their own
configurable limits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from src.core.config import Settings, settings
from src.models.evaluation import EvaluationStatus
from src.tasks import evaluation_tasks as et

pytestmark = pytest.mark.unit

RUNNING = EvaluationStatus.RUNNING.value


def _job(status: str, *, claimed_by: Any = None, age_s: float = 10.0) -> MagicMock:
    job = MagicMock()
    job.status = status
    job.parameters = {} if claimed_by is None else {et._CLAIM_KEY: claimed_by}
    job.started_at = datetime.now(timezone.utc) - timedelta(seconds=age_s)

    def fail_job(message: str) -> None:
        job.status = EvaluationStatus.FAILED.value
        job.error_message = message

    job.fail_job.side_effect = fail_job
    return job


def _db(job: Any, dataset: Any = None) -> MagicMock:
    db = MagicMock()
    chain = db.query.return_value.filter.return_value
    chain.with_for_update.return_value.first.return_value = job
    chain.first.return_value = job
    chain.order_by.return_value.first.return_value = dataset
    return db


def _dataset(n: int) -> SimpleNamespace:
    return SimpleNamespace(
        questions=[f"q{i}" for i in range(n)],
        reference_answers=[],
        contexts=[[f"c{i}"] for i in range(n)],
    )


# ---- claim decisions --------------------------------------------------------


def test_fresh_job_is_claimed_and_stamped() -> None:
    job = _job(EvaluationStatus.PENDING.value)
    db = _db(job)

    claimed, skip = et._claim_evaluation_job(db, "job-1", "task-A")
    assert claimed is job and skip is None
    db.query.return_value.filter.return_value.with_for_update.assert_called_once()

    et._start_claimed_job(job, "task-A")
    job.start_job.assert_called_once()
    assert job.parameters[et._CLAIM_KEY] == "task-A"


def test_redelivery_of_the_same_task_fails_the_job_instead_of_rerunning() -> None:
    job = _job(RUNNING, claimed_by="task-A")
    db = _db(job)

    _, skip = et._claim_evaluation_job(db, "job-1", "task-A")

    assert skip == {"status": "failed", "job_id": "job-1", "skipped": "worker_lost"}
    assert "not restarted" in job.error_message
    job.start_job.assert_not_called()
    db.commit.assert_called_once()


def test_running_job_owned_by_another_delivery_is_left_alone() -> None:
    job = _job(RUNNING, claimed_by="task-A")
    db = _db(job)

    _, skip = et._claim_evaluation_job(db, "job-1", "task-B")

    assert skip is not None and skip["skipped"] == "running_elsewhere"
    job.fail_job.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_called_once()


def test_unclaimed_running_job_older_than_the_hard_limit_is_failed() -> None:
    job = _job(RUNNING, age_s=settings.EVALUATION_TASK_TIME_LIMIT_SECONDS + 60)
    _, skip = et._claim_evaluation_job(_db(job), "job-1", "task-B")
    assert skip is not None and skip["skipped"] == "worker_lost"


def test_unclaimed_young_running_job_is_left_alone() -> None:
    job = _job(RUNNING, age_s=30)
    _, skip = et._claim_evaluation_job(_db(job), "job-1", "task-B")
    assert skip is not None and skip["skipped"] == "running_elsewhere"


def test_missing_job_raises() -> None:
    with pytest.raises(ValueError):
        et._claim_evaluation_job(_db(None), "job-1", "task-A")


# ---- end to end through the Celery task ------------------------------------


def test_worker_lost_redelivery_runs_no_item(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job(RUNNING, claimed_by="task-A")
    db = _db(job, _dataset(3))
    monkeypatch.setattr(et, "SessionLocal", lambda: db)
    triad = MagicMock()
    monkeypatch.setattr(et.rag_evaluation_service, "run_rag_triad_evaluation", triad)

    result = et.run_rag_triad_evaluation.apply(args=("job-1",), task_id="task-A").result

    assert result["skipped"] == "worker_lost"
    triad.assert_not_called()
    job.start_job.assert_not_called()


def test_batch_worker_lost_redelivery_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job(RUNNING, claimed_by="task-A")
    db = _db(job)
    monkeypatch.setattr(et, "SessionLocal", lambda: db)
    pipeline = MagicMock()
    monkeypatch.setattr(et.rag_evaluation_service, "evaluate_search_pipeline", pipeline)

    result = et.run_batch_evaluation.apply(
        args=("job-2", ["q1"]), task_id="task-A"
    ).result

    assert result["skipped"] == "worker_lost"
    pipeline.assert_not_called()


def test_real_time_redelivery_of_a_running_job_fails_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _job(RUNNING)
    existing.id = "job-3"
    db = _db(existing)
    monkeypatch.setattr(et, "SessionLocal", lambda: db)

    result = et.run_real_time_evaluation.apply(
        args=("q", "a", ["ctx"]), task_id="task-A"
    ).result

    assert result == {"status": "failed", "job_id": "job-3", "skipped": "worker_lost"}
    db.add.assert_not_called()


# ---- time limits ------------------------------------------------------------

EVAL_TASKS = [
    et.run_rag_triad_evaluation,
    et.run_batch_evaluation,
    et.run_real_time_evaluation,
    et.run_comparison_evaluation,
]


@pytest.mark.parametrize("task", EVAL_TASKS, ids=lambda t: t.name.rsplit(".", 1)[-1])
def test_evaluation_tasks_carry_configured_limits(task: Any) -> None:
    assert task.soft_time_limit == settings.EVALUATION_TASK_SOFT_TIME_LIMIT_SECONDS
    assert task.time_limit == settings.EVALUATION_TASK_TIME_LIMIT_SECONDS
    # Far above the global 300s soft limit that #2208 started enforcing.
    assert task.soft_time_limit >= 1800
    assert task.soft_time_limit < task.time_limit


def test_soft_limit_must_be_below_hard_limit() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {
                "EVALUATION_TASK_SOFT_TIME_LIMIT_SECONDS": 3000,
                "EVALUATION_TASK_TIME_LIMIT_SECONDS": 3000,
            }
        )


def test_hard_limit_must_stay_under_the_broker_visibility_timeout() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"EVALUATION_TASK_TIME_LIMIT_SECONDS": 3600})
