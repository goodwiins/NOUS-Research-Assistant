"""GOO-464: KG job endpoints commit the job row before publishing the task.

``create_merge_job`` and ``create_extraction_job`` used to flush, publish the
Celery message, then commit. A fast worker could receive the message and look
up a job row that was not committed yet. They now reserve the attempt durably
first (pre-generated task id + QUEUED, committed), publish with that task id,
and mark the reserved job FAILED when publication raises.
"""

from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.core.database import get_db_sync
from src.core.dependencies import get_current_user
from src.models.processing import JobStatus

pytestmark = pytest.mark.unit


@pytest.fixture
def events() -> list[Any]:
    return []


@pytest.fixture
def added() -> list[Any]:
    return []


@pytest.fixture
def mock_sync_db(events: list[Any], added: list[Any]) -> MagicMock:
    db = MagicMock()
    db.add.side_effect = added.append
    db.flush.side_effect = lambda *a, **k: events.append("flush")
    db.commit.side_effect = lambda *a, **k: events.append("commit")
    db.refresh.side_effect = lambda *a, **k: events.append("refresh")
    return db


@pytest.fixture(autouse=True)
def _override_dependencies(
    test_app: Any, mock_user: Any, mock_sync_db: MagicMock
) -> Iterator[None]:
    test_app.dependency_overrides[get_current_user] = lambda: mock_user

    def override_get_db_sync() -> Iterator[MagicMock]:
        yield mock_sync_db

    test_app.dependency_overrides[get_db_sync] = override_get_db_sync
    try:
        yield
    finally:
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db_sync, None)


def _publisher(events: list[Any]) -> MagicMock:
    def publish(*args: Any, **kwargs: Any) -> MagicMock:
        events.append(("publish", kwargs.get("task_id")))
        return MagicMock(id=kwargs.get("task_id"))

    return MagicMock(side_effect=publish)


MERGE_PAYLOAD = {
    "groups": [
        {
            "entities": [{"id": "entity-1", "name": "A"}],
            "suggested_primary": "entity-1",
        }
    ]
}


def _post_merge(test_client: Any, mock_sync_db: MagicMock) -> Any:
    # No cross-org source documents.
    mock_sync_db.query.return_value.filter.return_value.all.return_value = []
    with patch(
        "src.api.search.knowledge_graph.knowledge_graph_service.get_entity",
        return_value=MagicMock(source_document_id=None),
    ):
        return test_client.post(
            "/api/v1/knowledge-graph/merge-jobs", json=MERGE_PAYLOAD
        )


def _post_extraction(test_client: Any, mock_sync_db: MagicMock) -> Any:
    doc = MagicMock()
    doc.id = "doc-1"
    mock_sync_db.query.return_value.filter.return_value.all.return_value = [doc]
    return test_client.post(
        "/api/v1/knowledge-graph/extraction-jobs", json={"document_ids": ["doc-1"]}
    )


CASES = [
    ("kg_merge_entities_job", _post_merge),
    ("kg_extract_entities_job", _post_extraction),
]


@pytest.mark.parametrize("task_attr,post", CASES)
def test_job_row_is_committed_before_the_task_is_published(
    task_attr: str,
    post: Callable[[Any, MagicMock], Any],
    test_client: Any,
    mock_sync_db: MagicMock,
    events: list[Any],
    added: list[Any],
) -> None:
    publisher = _publisher(events)
    with patch(f"src.api.search.knowledge_graph.{task_attr}.apply_async", publisher):
        response = post(test_client, mock_sync_db)

    assert response.status_code == 202, response.text
    publishes = [e for e in events if isinstance(e, tuple)]
    assert len(publishes) == 1
    assert "commit" in events
    assert events.index("commit") < events.index(publishes[0]), (
        "the job row must be durable before a worker can receive its message; "
        f"observed order {events}"
    )
    job = added[0]
    # The published message carries the id persisted on the reserved row.
    assert publishes[0][1] is not None
    assert publishes[0][1] == job.celery_task_id
    assert job.status is JobStatus.QUEUED
    assert response.json()["status"] == "queued"


@pytest.mark.parametrize("task_attr,post", CASES)
def test_publish_failure_fails_the_reserved_job(
    task_attr: str,
    post: Callable[[Any, MagicMock], Any],
    test_client: Any,
    mock_sync_db: MagicMock,
    events: list[Any],
    added: list[Any],
) -> None:
    with patch(
        f"src.api.search.knowledge_graph.{task_attr}.apply_async",
        side_effect=RuntimeError("broker down"),
    ):
        response = post(test_client, mock_sync_db)

    assert response.status_code == 503
    assert "broker down" not in response.text
    job = added[0]
    assert job.status is JobStatus.FAILED
    # Reservation commit, then the failure commit.
    assert events.count("commit") == 2
