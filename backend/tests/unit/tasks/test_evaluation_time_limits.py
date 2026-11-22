"""Evaluation job timestamps, soft time limit and event loop (GOO-645).

MS2A-1: ``EvaluationJob.complete_job/fail_job/cancel_job`` subtracted a naive
``datetime.utcnow()`` from ``started_at`` (timestamptz, re-read as aware after
a commit), raising ``TypeError`` once the paid LLM work was done. The failure
handler hit the same error, so the job stayed RUNNING.

CI2C-3: the per-item loop caught ``SoftTimeLimitExceeded`` with ``except
Exception: continue``. The hard kill followed, the job was still RUNNING, and
the acks_late redelivery re-ran every paid item.

CI-3: tasks built and closed private event loops instead of the shared
``run_async`` boundary.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from src.models.evaluation import EvaluationJob, EvaluationStatus
from src.tasks import evaluation_tasks as et

pytestmark = pytest.mark.unit


# ---- MS2A-1: aware timestamps ---------------------------------------------


@pytest.mark.parametrize("aware", [True, False], ids=["aware", "naive"])
@pytest.mark.parametrize("finish", ["complete", "fail", "cancel"])
def test_finishing_a_job_computes_duration(aware: bool, finish: str) -> None:
    # Build the timestamp inside the test: a parametrize-time value is taken at
    # collection, minutes before the test runs on a large suite.
    now = datetime.now(timezone.utc)
    started_at = now if aware else now.replace(tzinfo=None)  # aware = re-read from PG
    job = EvaluationJob(name="j", evaluation_type="rag_triad", parameters={})
    job.started_at = started_at - timedelta(seconds=30)  # type: ignore[assignment]

    if finish == "complete":
        job.complete_job(overall_score=0.9, success_rate=100.0)
    elif finish == "fail":
        job.fail_job("boom")
    else:
        job.cancel_job()

    assert job.completed_at is not None and job.completed_at.tzinfo is not None
    assert 29 <= job.duration_seconds < 120


def test_start_job_is_timezone_aware() -> None:
    job = EvaluationJob(name="j", evaluation_type="rag_triad", parameters={})
    job.start_job()
    assert job.status == EvaluationStatus.RUNNING.value
    assert job.started_at.tzinfo is not None


# ---- CI2C-3: soft time limit propagates ------------------------------------


def _db_with(job: Any, dataset: Any) -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = job
    db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = (
        job
    )
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
        dataset
    )
    return db


def test_soft_time_limit_stops_the_loop_and_fails_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = MagicMock()
    job.status = "pending"
    job.organization_id = "org"
    job.user_id = "user"
    job.parameters = {}
    dataset = MagicMock()
    dataset.questions = ["q1", "q2", "q3"]
    dataset.reference_answers = []
    dataset.contexts = [["c1"], ["c2"], ["c3"]]
    db = _db_with(job, dataset)
    monkeypatch.setattr(et, "SessionLocal", lambda: db)

    metrics = MagicMock(overall_score=0.9)
    triad = AsyncMock(side_effect=[metrics, SoftTimeLimitExceeded(), metrics])
    monkeypatch.setattr(et.rag_evaluation_service, "run_rag_triad_evaluation", triad)
    monkeypatch.setattr(
        et.rag_evaluation_service, "_generate_simple_answer", lambda q, c: "a"
    )

    result = et.run_rag_triad_evaluation.apply(args=("job-1",))

    assert isinstance(result.result, SoftTimeLimitExceeded)
    assert triad.await_count == 2  # item 3 never ran
    job.complete_job.assert_not_called()
    (message,), _ = job.fail_job.call_args
    assert "time limit" in message


def test_ordinary_item_error_still_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    job = MagicMock()
    job.status = "pending"
    job.parameters = {}
    job.duration_seconds = 1.0
    dataset = MagicMock()
    dataset.questions = ["q1", "q2"]
    dataset.reference_answers = []
    dataset.contexts = [["c1"], ["c2"]]
    db = _db_with(job, dataset)
    monkeypatch.setattr(et, "SessionLocal", lambda: db)

    metrics = MagicMock(overall_score=0.9, answer_relevancy=1.0, faithfulness=1.0)
    metrics.contextual_relevancy = 1.0
    triad = AsyncMock(side_effect=[RuntimeError("llm 500"), metrics])
    monkeypatch.setattr(et.rag_evaluation_service, "run_rag_triad_evaluation", triad)
    monkeypatch.setattr(
        et.rag_evaluation_service, "_generate_simple_answer", lambda q, c: "a"
    )

    result = et.run_rag_triad_evaluation.apply(args=("job-2",)).result

    assert result["status"] == "completed"
    assert result["processed_items"] == 1
    assert triad.await_count == 2


def test_failure_message_for_other_errors_is_the_error_text() -> None:
    assert et._failure_message(ValueError("bad dataset")) == "bad dataset"


# ---- CI-3: shared event loop -----------------------------------------------


def test_tasks_use_the_shared_run_async_boundary() -> None:
    source = inspect.getsource(et)
    assert "new_event_loop" not in source
    assert "run_until_complete" not in source
    assert et.run_async.__module__ == "src.tasks._async_utils"
