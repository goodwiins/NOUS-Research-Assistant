"""Evaluation follow-ups from the review of #2235 (GOO-696).

* ``EvaluationJob.error_message`` is shown to users, so an unexpected
  exception is stored as a fixed message with an error code; the exception
  text goes to the server log only. Messages the tasks write on purpose
  (``EvaluationFailure``) are kept.
* A rag-triad item without context is scored by the search pipeline; the task
  used to judge it a second time (four more LLM calls) and save both sets.
  It now saves the pipeline's scores once.
* Batch and rag-triad items write the pipeline's metric rows in the task's
  session, so a failed summary or progress commit rolls back the whole query.
* The real-time advisory lock key uses the 64-bit ``hashtextextended``.

The real-Postgres halves (rollback of the service rows, two racing
deliveries) are in ``test_evaluation_metrics_postgres.py``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.evaluation import EvaluationStatus
from src.services.evaluation import rag_evaluation_service as svc_mod
from src.tasks import evaluation_tasks as et

pytestmark = pytest.mark.unit

FAILED = EvaluationStatus.FAILED.value
SECRET = "connection to 10.0.0.5 failed: password=hunter2"


def _job() -> MagicMock:
    job = MagicMock()
    job.status = EvaluationStatus.PENDING.value
    job.error_message = None
    job.parameters = {}
    job.organization_id = "org"
    job.user_id = "user"

    def fail_job(message: str) -> None:
        job.status = FAILED
        job.error_message = message

    job.fail_job.side_effect = fail_job
    return job


def _db(job: Any, dataset: Any = None) -> MagicMock:
    db = MagicMock()
    chain = db.query.return_value.filter.return_value
    chain.first.return_value = job
    chain.with_for_update.return_value.first.return_value = job
    chain.order_by.return_value.first.return_value = dataset
    return db


def _dataset(
    questions: list[str], contexts: list[Any], references: list[Any]
) -> SimpleNamespace:
    return SimpleNamespace(
        questions=questions, contexts=contexts, reference_answers=references
    )


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


# ---- error_message is a safe, fixed text ------------------------------------


def test_failure_message_hides_unexpected_error_text() -> None:
    message = et._failure_message(RuntimeError(SECRET))

    assert "hunter2" not in message
    assert "10.0.0.5" not in message
    assert message == et._UNEXPECTED_FAILURE_MESSAGE
    assert "error code" in message


def test_failure_message_keeps_the_tasks_own_messages() -> None:
    exc = et.EvaluationFailure("No items were successfully processed")

    assert et._failure_message(exc) == "No items were successfully processed"
    assert isinstance(exc, ValueError)  # callers catching ValueError still work


def test_rag_triad_job_stores_a_safe_message_and_logs_the_detail(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    job = _job()
    db = _db(job)
    db.query.return_value.filter.return_value.order_by.return_value.first.side_effect = RuntimeError(
        SECRET
    )
    monkeypatch.setattr(et, "SessionLocal", lambda: db)

    with caplog.at_level(logging.ERROR, logger=et.logger.name):
        et.run_rag_triad_evaluation.apply(args=("job-1",), task_id="task-A")

    assert job.status == FAILED
    assert job.error_message == et._UNEXPECTED_FAILURE_MESSAGE
    assert "hunter2" in caplog.text  # the detail stays in the server log


def test_rag_triad_no_items_keeps_its_specific_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    db = _db(job, _dataset(["q1"], [["ctx"]], [None]))
    monkeypatch.setattr(et, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        et.rag_evaluation_service,
        "run_rag_triad_evaluation",
        AsyncMock(side_effect=RuntimeError("llm 500")),
    )
    monkeypatch.setattr(
        et.rag_evaluation_service, "_generate_simple_answer", lambda q, c: "a"
    )

    et.run_rag_triad_evaluation.apply(args=("job-1",), task_id="task-A")

    assert job.error_message == "No items were successfully processed"


# ---- no-context items are judged once ---------------------------------------


def test_rag_triad_no_context_item_reuses_the_pipeline_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    db = _db(job, _dataset(["q1"], [], ["ref answer"]))
    monkeypatch.setattr(et, "SessionLocal", lambda: db)
    pipeline = AsyncMock(return_value=[_metrics(0.8)])
    triad = AsyncMock(return_value=_metrics(0.1))
    monkeypatch.setattr(et.rag_evaluation_service, "evaluate_search_pipeline", pipeline)
    monkeypatch.setattr(et.rag_evaluation_service, "run_rag_triad_evaluation", triad)

    result = et.run_rag_triad_evaluation.apply(args=("job-1",), task_id="task-A").result

    triad.assert_not_awaited()  # the pipeline already judged it
    assert pipeline.await_args is not None
    kwargs = pipeline.await_args.kwargs
    assert kwargs["job_id"] == "job-1"  # the pipeline saves the rows ...
    assert kwargs["db"] is db  # ... in the task's transaction
    assert kwargs["reference_answers"] == ["ref answer"]
    assert result["processed_items"] == 1
    assert result["overall_score"] == pytest.approx(0.8)


def test_rag_triad_item_with_context_is_still_judged_by_the_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    db = _db(job, _dataset(["q1"], [["ctx"]], [None]))
    monkeypatch.setattr(et, "SessionLocal", lambda: db)
    pipeline = AsyncMock()
    triad = AsyncMock(return_value=_metrics(0.6))
    monkeypatch.setattr(et.rag_evaluation_service, "evaluate_search_pipeline", pipeline)
    monkeypatch.setattr(et.rag_evaluation_service, "run_rag_triad_evaluation", triad)
    monkeypatch.setattr(
        et.rag_evaluation_service, "_generate_simple_answer", lambda q, c: "a"
    )

    result = et.run_rag_triad_evaluation.apply(args=("job-1",), task_id="task-A").result

    pipeline.assert_not_awaited()
    triad.assert_awaited_once()
    assert result["overall_score"] == pytest.approx(0.6)


def test_rag_triad_item_commit_failure_is_rolled_back_and_the_run_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    db = _db(job, _dataset(["q1", "q2"], [["c1"], ["c2"]], [None, None]))
    events: list[str] = []

    def commit() -> None:
        events.append("commit")
        if events.count("commit") == 2:  # the claim commits first; q1's fails
            raise RuntimeError("metric insert failed")

    db.commit.side_effect = commit
    db.rollback.side_effect = lambda: events.append("rollback")
    monkeypatch.setattr(et, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        et.rag_evaluation_service,
        "run_rag_triad_evaluation",
        AsyncMock(side_effect=[_metrics(0.9), _metrics(0.7)]),
    )
    monkeypatch.setattr(
        et.rag_evaluation_service, "_generate_simple_answer", lambda q, c: "a"
    )

    result = et.run_rag_triad_evaluation.apply(args=("job-1",), task_id="task-A").result

    assert events[:3] == ["commit", "commit", "rollback"]
    assert result["processed_items"] == 1
    assert result["overall_score"] == pytest.approx(0.7)  # q1 is not counted


# ---- batch writes the pipeline rows in its own transaction ------------------


def test_batch_pipeline_writes_into_the_task_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(_job())
    monkeypatch.setattr(et, "SessionLocal", lambda: db)
    pipeline = AsyncMock(return_value=[_metrics(0.8)])
    monkeypatch.setattr(et.rag_evaluation_service, "evaluate_search_pipeline", pipeline)

    et.run_batch_evaluation.apply(args=("job-2", ["q1"]), task_id="task-A")

    assert pipeline.await_args is not None
    assert pipeline.await_args.kwargs["db"] is db


@pytest.fixture
def judged(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    service = svc_mod.rag_evaluation_service
    for name in (
        "_calculate_answer_relevancy",
        "_calculate_faithfulness",
        "_calculate_contextual_relevancy",
        "_calculate_hallucination_rate",
    ):
        monkeypatch.setattr(service, name, AsyncMock(return_value=0.8))
    monkeypatch.setattr(service, "_get_threshold", lambda *a, **k: None)
    hit = SimpleNamespace(content_preview="ctx", document_id="d1")
    response = SimpleNamespace(
        results=[hit], search_time_ms=1.0, model_dump=lambda: {"results": []}
    )
    search = MagicMock(return_value=response)
    monkeypatch.setattr(svc_mod.hybrid_search_service, "search", search)
    own_session = MagicMock()
    monkeypatch.setattr(svc_mod, "SessionLocal", lambda: own_session)
    return own_session


async def test_pipeline_with_a_caller_session_flushes_and_leaves_it_open(
    judged: MagicMock,
) -> None:
    db = MagicMock()
    added: list[Any] = []
    db.add.side_effect = added.append

    results = await svc_mod.rag_evaluation_service.evaluate_search_pipeline(
        ["q1"], "org", "user", job_id="job-1", db=db, reference_answers=["ref"]
    )

    assert len(results) == 1
    assert len(added) == 4
    assert {m.reference_answer for m in added} == {"ref"}
    db.flush.assert_called()
    db.commit.assert_not_called()  # the caller commits with its summary row
    db.close.assert_not_called()
    judged.add.assert_not_called()  # no second session was used


async def test_pipeline_rolls_back_the_caller_session_when_a_query_fails(
    judged: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = svc_mod.rag_evaluation_service
    monkeypatch.setattr(
        service,
        "_calculate_hallucination_rate",
        AsyncMock(side_effect=RuntimeError("judge down")),
    )
    db = MagicMock()

    results = await service.evaluate_search_pipeline(
        ["q1"], "org", "user", job_id="job-1", db=db
    )

    assert results == []
    db.rollback.assert_called_once()


async def test_pipeline_without_a_caller_session_commits_and_closes_its_own(
    judged: MagicMock,
) -> None:
    await svc_mod.rag_evaluation_service.evaluate_search_pipeline(
        ["q1"], "org", "user", job_id="job-1"
    )

    judged.commit.assert_called_once()
    judged.close.assert_called_once()


# ---- 64-bit advisory lock key -----------------------------------------------


def test_real_time_lock_uses_the_64_bit_hash() -> None:
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "postgresql"

    et._lock_real_time_delivery(db, "task-A")

    [statement] = [str(c.args[0]) for c in db.execute.call_args_list]
    assert "pg_advisory_xact_lock(hashtextextended(:key, 0))" in statement
