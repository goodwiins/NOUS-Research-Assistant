"""GOO-464: KG workers re-check the job under a row lock before starting it.

The KG job endpoints reserve a job (QUEUED + task id, committed) and publish
afterwards; if the publish raises they fail that reservation. A broker can
accept a message and still raise, so a worker may read the QUEUED row without
a lock while the producer commits FAILED. A bare ``start_job()`` on that stale
object then wrote RUNNING over FAILED and ran a job the client was told had
failed. Both workers now refresh the row ``with_for_update`` and skip a
terminal job before ``start_job()``. The extraction worker also used to skip
only COMPLETED, so it restarted FAILED / CANCELLED jobs outright.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.models.processing import JobStatus
from src.tasks import processing_tasks as pt

pytestmark = pytest.mark.unit

_TERMINAL = [JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.COMPLETED]


def _job(status: JobStatus) -> MagicMock:
    job = MagicMock()
    job.status = status
    job.organization_id = "org-1"
    job.parameters = {
        "groups": [
            {
                "suggested_primary": "primary-1",
                "entities": [{"id": "primary-1"}, {"id": "dup-1"}],
            }
        ],
        "document_ids": ["doc-1"],
    }
    return job


def _db_for(job: MagicMock, status_under_lock: JobStatus) -> MagicMock:
    """A session whose unlocked read sees ``job`` as-is and whose locked
    refresh sees the producer's concurrent write."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [job]

    def locked_refresh(instance: Any, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("with_for_update"):
            instance.status = status_under_lock

    db.refresh.side_effect = locked_refresh
    return db


@pytest.mark.parametrize("terminal", _TERMINAL)
@pytest.mark.parametrize(
    "task_name", ["kg_merge_entities_job", "kg_extract_entities_job"]
)
def test_worker_skips_a_job_that_turned_terminal_before_its_locked_start(
    monkeypatch: pytest.MonkeyPatch, task_name: str, terminal: JobStatus
) -> None:
    job = _job(JobStatus.QUEUED)
    db = _db_for(job, status_under_lock=terminal)
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)
    kg = MagicMock()
    monkeypatch.setattr(pt, "knowledge_graph_service", kg)

    result = getattr(pt, task_name).apply(args=("job-1",)).result

    assert result["skipped"] == "duplicate_delivery"
    assert result["status"] == terminal.value
    db.refresh.assert_any_call(job, with_for_update=True)
    job.start_job.assert_not_called()
    assert kg.method_calls == []


@pytest.mark.parametrize("terminal", [JobStatus.FAILED, JobStatus.CANCELLED])
def test_extraction_worker_skips_a_failed_or_cancelled_job(
    monkeypatch: pytest.MonkeyPatch, terminal: JobStatus
) -> None:
    job = _job(terminal)
    db = _db_for(job, status_under_lock=terminal)
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)

    result = pt.kg_extract_entities_job.apply(args=("job-2",)).result

    assert result["skipped"] == "duplicate_delivery"
    job.start_job.assert_not_called()


@pytest.mark.parametrize(
    "task_name", ["kg_merge_entities_job", "kg_extract_entities_job"]
)
def test_worker_still_starts_a_live_job_under_the_lock(
    monkeypatch: pytest.MonkeyPatch, task_name: str
) -> None:
    job = _job(JobStatus.QUEUED)
    db = _db_for(job, status_under_lock=JobStatus.QUEUED)
    monkeypatch.setattr(pt, "SessionLocal", lambda: db)
    monkeypatch.setattr(pt, "knowledge_graph_service", MagicMock())
    monkeypatch.setattr(pt, "LLMEntityExtractionService", MagicMock())
    monkeypatch.setattr(pt, "run_async", MagicMock(side_effect=RuntimeError("stop")))

    getattr(pt, task_name).apply(args=("job-3",))

    db.refresh.assert_any_call(job, with_for_update=True)
    job.start_job.assert_called_once()
