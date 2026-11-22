"""Real-Postgres proof for evaluation metric writes (GOO-689).

``evaluation_metrics.job_id`` is NOT NULL. ``evaluate_search_pipeline`` called
``run_rag_triad_evaluation`` with ``job_id=None``, which added four metric rows
and committed. On Postgres that commit raised ``NotNullViolation``, the
pipeline's per-query handler swallowed it, and every query was dropped: a batch
evaluation could only ever end "No queries were successfully evaluated".
SQLite unit tests with a mocked session never saw it.

Also proves the real-time task's advisory lock on its Celery task id: a second
delivery cannot take it while the first holds it.

Needs ``ORCHESTRATION_TEST_DATABASE_URL`` (a disposable database; each test
uses its own schema).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import src.models  # noqa: F401  register every table on Base.metadata
from src.models.base import Base
from src.models.evaluation import (
    EvaluationJob,
    EvaluationMetric,
    EvaluationStatus,
    EvaluationThreshold,
    EvaluationType,
)
from src.models.organization import Organization
from src.models.user import User
from src.services.evaluation import rag_evaluation_service as svc_mod
from src.tasks import evaluation_tasks as et

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


def _dsn() -> str:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg2://" + dsn[len(prefix) :]
    raise ValueError("ORCHESTRATION_TEST_DATABASE_URL must be a PostgreSQL URL")


@contextmanager
def _schema(
    status: str = EvaluationStatus.RUNNING.value,
) -> Iterator[SimpleNamespace]:
    """Isolated schema with one organization and one batch job."""
    schema = "eval_metrics_" + uuid.uuid4().hex
    admin = create_engine(_dsn())
    with admin.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(_dsn(), connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                Organization.__table__,
                User.__table__,
                EvaluationJob.__table__,
                EvaluationMetric.__table__,
                EvaluationThreshold.__table__,
            ],
        )
        factory = sessionmaker(engine, autocommit=False, autoflush=False)
        org_id, job_id = uuid.uuid4(), uuid.uuid4()
        with factory() as db:
            db.add(Organization(id=org_id, name="Org", storage_limit_bytes=10_000))
            db.flush()
            db.add(
                EvaluationJob(
                    id=job_id,
                    name="batch",
                    evaluation_type=EvaluationType.BATCH_EVALUATION.value,
                    status=status,
                    organization_id=org_id,
                    parameters={},
                )
            )
            db.commit()
        yield SimpleNamespace(factory=factory, org_id=org_id, job_id=job_id)
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


@pytest.fixture
def judged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic judge scores and one search hit; no model or index calls."""
    service = svc_mod.rag_evaluation_service
    for name in (
        "_calculate_answer_relevancy",
        "_calculate_faithfulness",
        "_calculate_contextual_relevancy",
        "_calculate_hallucination_rate",
    ):
        monkeypatch.setattr(service, name, AsyncMock(return_value=0.8))
    hit = SimpleNamespace(content_preview="ctx", document_id="d1")
    response = SimpleNamespace(
        results=[hit], search_time_ms=1.0, model_dump=lambda: {"results": []}
    )
    monkeypatch.setattr(
        svc_mod.hybrid_search_service, "search", MagicMock(return_value=response)
    )


def _metric_job_ids(factory: sessionmaker[Session]) -> list[Any]:
    with factory() as db:
        return [row[0] for row in db.query(EvaluationMetric.job_id).all()]


def test_pipeline_with_a_job_writes_its_metrics(
    judged: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _schema() as s:
        monkeypatch.setattr(svc_mod, "SessionLocal", s.factory)

        results = asyncio.run(
            svc_mod.rag_evaluation_service.evaluate_search_pipeline(
                ["q1", "q2"], str(s.org_id), "anonymous", job_id=str(s.job_id)
            )
        )

        assert len(results) == 2  # develop: 0 (every query dropped)
        job_ids = _metric_job_ids(s.factory)
        assert len(job_ids) == 8  # four triad metrics per query
        assert set(job_ids) == {s.job_id}


def test_pipeline_without_a_job_returns_scores_and_writes_nothing(
    judged: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _schema() as s:
        monkeypatch.setattr(svc_mod, "SessionLocal", s.factory)

        results = asyncio.run(
            svc_mod.rag_evaluation_service.evaluate_search_pipeline(
                ["q1"], str(s.org_id), "anonymous"
            )
        )

        assert [r.overall_score for r in results] == [pytest.approx(0.8)]
        assert _metric_job_ids(s.factory) == []


def test_real_time_task_lock_blocks_a_second_delivery() -> None:
    with _schema() as s:
        with s.factory() as first, s.factory() as second:
            et._lock_real_time_delivery(first, "task-A")

            second.execute(text("SET LOCAL lock_timeout = '200ms'"))
            with pytest.raises(OperationalError):
                et._lock_real_time_delivery(second, "task-A")
            second.rollback()

            # A different delivery is not blocked, and committing releases A.
            et._lock_real_time_delivery(second, "task-B")
            second.rollback()
            first.commit()
            second.execute(text("SET LOCAL lock_timeout = '200ms'"))
            et._lock_real_time_delivery(second, "task-A")  # no longer blocked
            second.rollback()


# ---- GOO-696 ----------------------------------------------------------------


def _rows_per_query(factory: sessionmaker[Session]) -> dict[str, int]:
    with factory() as db:
        counts: dict[str, int] = {}
        for (query,) in db.query(EvaluationMetric.query).all():
            counts[query] = counts.get(query, 0) + 1
        return counts


def test_failed_batch_summary_rolls_back_the_querys_service_rows(
    judged: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """q1's summary row violates NOT NULL at commit. Its four service rows
    used to stay committed (the service committed them in its own session)
    while the query was not counted; now all five roll back together."""
    with _schema(EvaluationStatus.PENDING.value) as s:
        monkeypatch.setattr(et, "SessionLocal", s.factory)
        monkeypatch.setattr(svc_mod, "SessionLocal", s.factory)

        def summary_metric(**fields: Any) -> EvaluationMetric:
            if fields["query"] == "q1":
                fields["job_id"] = None  # NOT NULL: fails at commit
            return EvaluationMetric(**fields)

        monkeypatch.setattr(et, "EvaluationMetric", summary_metric)

        result = et.run_batch_evaluation.apply(
            args=(str(s.job_id), ["q1", "q2"]), task_id="task-batch"
        ).result

        assert result["processed_queries"] == 1
        assert _rows_per_query(s.factory) == {"q2": 5}  # 4 triad + 1 summary


def test_two_concurrent_real_time_deliveries_create_one_job(
    judged: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two deliveries of one task id really overlap: each opens its session
    and holds a connection before either goes on (else one thread can reuse
    the pooled connection and finish before the other starts), and the window
    between the job lookup and the commit that creates the job is widened.
    With the lock the second delivery waits, then finds the first one's job.
    Without it both find none and each creates a job and pays for a
    judgement.

    The sync points are the session and ``start_job``, not the lock helper,
    so the test still races when the lock call itself is removed.
    """
    with _schema() as s:
        barrier = threading.Barrier(2)
        opened: list[Session] = []
        opened_guard = threading.Lock()

        def racing_session() -> Session:
            db = s.factory()
            with opened_guard:
                first_two = len(opened) < 2
                opened.append(db)
            if first_two:  # the two deliveries; any later session is free
                db.execute(text("SELECT 1"))  # hold a real connection
                barrier.wait(timeout=10)
            return db

        monkeypatch.setattr(et, "SessionLocal", racing_session)
        start_job = EvaluationJob.start_job

        def slow_start_job(job: EvaluationJob) -> None:
            start_job(job)
            time.sleep(0.3)  # after the lookup, before the creating commit

        monkeypatch.setattr(EvaluationJob, "start_job", slow_start_job)
        states: list[str] = []

        def deliver() -> None:
            outcome = et.run_real_time_evaluation.apply(
                args=("q", "a", ["ctx"]),
                kwargs={"organization_id": str(s.org_id)},
                task_id="task-race",
            )
            states.append(outcome.state)

        threads = [threading.Thread(target=deliver) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert sorted(states) == ["IGNORED", "SUCCESS"]
        with s.factory() as db:
            jobs = (
                db.query(EvaluationJob)
                .filter(
                    EvaluationJob.evaluation_type
                    == EvaluationType.REAL_TIME_EVALUATION.value
                )
                .all()
            )
            assert len(jobs) == 1
            assert db.query(EvaluationMetric).count() == 4  # judged once
