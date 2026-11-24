"""Queued Bedrock KB write path (``src.services.bedrock_sync``).

Uses the in-memory SQLite harness from ``tests/unit/tasks/test_satellite_status.py``
with fake S3 and fake ``bedrock-agent`` clients; nothing touches the network.

Mutation-verified guards (docs/engineering/testing.md), focused command:
    pytest -q backend/tests/unit/services/test_bedrock_sync.py
- claim rule: ``_claim_new_job`` stamps the job on the *selected* ids whose
  ``stage_token`` is unchanged. Stamping every pending row, or dropping the
  token comparison, fails ``test_document_staged_after_selection_is_not_claimed``.
- settle re-check: ``_settle_job`` skips a row whose claim no longer names the
  job (restaged meanwhile); dropping that comparison fails
  ``test_restage_during_settlement_is_not_marked_completed``.
- single runner: ``run_locked_tick`` returns before touching the session or
  client when the advisory lock is not acquired; ignoring ``acquired`` fails
  ``test_tick_does_nothing_without_the_advisory_lock``.
- idempotency: ``_settle_claimed`` stops the tick while the claimed job runs;
  treating a running job as settled fails
  ``test_running_job_blocks_a_second_start``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Optional
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from src.models.document import Document, DocumentType, ProcessingStatus
from src.services import bedrock_sync as bs
from src.shared.enums import SatelliteSyncStatus
from src.tasks.bedrock_sync_tasks import run_locked_tick

pytestmark = pytest.mark.unit

PENDING = SatelliteSyncStatus.PENDING.value
COMPLETED = SatelliteSyncStatus.COMPLETED.value
FAILED = SatelliteSyncStatus.FAILED.value
PREFIX = "bedrock/nous-dev-knowledge-base/"


@pytest.fixture
def session_factory(tmp_path: Any) -> Any:
    """Sync sessions on a file DB; ``_tick`` opens aiosqlite on the same file."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import src.models  # noqa: F401  register every table on Base.metadata
    from src.models.base import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'docs.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def enabled() -> Any:
    with (
        patch.object(bs.settings, "BEDROCK_KB_ID", "KBID"),
        patch.object(bs.settings, "BEDROCK_KB_DATA_SOURCE_ID", "DSID"),
        patch.object(bs.settings, "BEDROCK_KB_S3_PREFIX", PREFIX),
    ):
        yield


class FakeS3:
    def __init__(self) -> None:
        self.puts: dict[str, bytes] = {}
        self.deletes: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.fail_upload = False
        self.fail_delete: set[str] = set()

    def upload_file(self, key: str, data: bytes, content_type: str = "") -> str:
        if self.fail_upload:
            raise RuntimeError("s3 down")
        self.puts[key] = data
        self.events.append(("put", key))
        return key

    def delete_file(self, key: str) -> bool:
        if key in self.fail_delete:
            return False
        self.deletes.append(key)
        self.events.append(("delete", key))
        return True


@pytest.fixture
def s3() -> Any:
    fake = FakeS3()
    with patch("src.core.s3_client.S3StorageHelper", return_value=fake):
        yield fake


class FakeBedrock:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.started: list[str] = []
        self.running_elsewhere = False
        self.conflict = False
        self.on_start: Optional[Callable[[], None]] = None
        self.calls: list[str] = []

    def get_ingestion_job(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("get")
        return {"ingestionJob": self.jobs[kwargs["ingestionJobId"]]}

    def list_ingestion_jobs(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("list")
        status = "IN_PROGRESS" if self.running_elsewhere else "COMPLETE"
        return {"ingestionJobSummaries": [{"status": status}]}

    def start_ingestion_job(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("start")
        assert kwargs["knowledgeBaseId"] == "KBID"
        assert kwargs["dataSourceId"] == "DSID"
        if self.on_start:
            self.on_start()
        if self.conflict:
            raise ClientError(
                {"Error": {"Code": "ConflictException", "Message": "busy"}},
                "StartIngestionJob",
            )
        job_id = f"job-{len(self.started) + 1}"
        self.started.append(job_id)
        self.jobs[job_id] = {
            "ingestionJobId": job_id,
            "status": "STARTING",
            "statistics": {"numberOfDocumentsFailed": 0},
        }
        return {"ingestionJob": dict(self.jobs[job_id])}


def _add(
    session_factory: Any, org_id: Any = None, *, text: Optional[str] = "body"
) -> Any:
    doc_id = uuid4()
    db = session_factory()
    db.add(
        Document(
            id=doc_id,
            title="Doc",
            filename="doc.txt",
            file_path="local:///tmp/doc.txt",
            file_size_bytes=10,
            mime_type="text/plain",
            document_type=DocumentType.TEXT,
            organization_id=org_id or uuid4(),
            content_text=text,
            processing_status=ProcessingStatus.COMPLETED,
        )
    )
    db.commit()
    db.close()
    return doc_id


def _stage(session_factory: Any, doc_id: Any) -> bs.StageOutcome:
    db = session_factory()
    try:
        outcome = bs.stage_document_sync(db, db.get(Document, doc_id))
        db.commit()
        return outcome
    finally:
        db.close()


def _load(session_factory: Any, doc_id: Any) -> Any:
    db = session_factory()
    try:
        return db.get(Document, doc_id)
    finally:
        db.close()


def _tick(session_factory: Any, client: Any, s3: Any) -> dict:
    """Run one async tick against the same SQLite file the sync helpers use."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    url = session_factory.kw["bind"].url.set(drivername="sqlite+aiosqlite")

    async def run() -> dict:
        engine = create_async_engine(url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                return await bs.run_sync_tick(db, client, helper_factory=lambda: s3)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _state(doc: Any) -> dict:
    return (doc.document_metadata or {}).get(bs.META_KEY) or {}


# --------------------------------------------------------------- disabled


def test_everything_is_a_no_op_when_not_enabled(session_factory: Any, s3: Any) -> None:
    doc_id = _add(session_factory)
    with patch.object(bs.settings, "BEDROCK_KB_ID", "KBID"):
        # A KB id alone (read path) does not turn the write path on.
        assert bs.bedrock_sync_enabled() is False
        assert _stage(session_factory, doc_id) is bs.StageOutcome.SKIPPED
        client = MagicMock()
        assert _tick(session_factory, client, s3)["skipped"] == "disabled"
        doc = _load(session_factory, doc_id)
        bs.mark_deleted(doc)
    assert s3.events == []
    client.assert_not_called()
    assert client.method_calls == []
    assert doc.do_kb_sync_status is None
    assert _load(session_factory, doc_id).do_kb_sync_status is None


# ----------------------------------------------------------------- staging


def test_stage_uploads_text_and_sidecar_then_marks_pending(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    org_id = uuid4()
    doc_id = _add(session_factory, org_id, text="hello")
    seen_status: list[Optional[str]] = []
    original = s3.upload_file

    def upload(key: str, data: bytes, content_type: str = "") -> str:
        seen_status.append(_load(session_factory, doc_id).do_kb_sync_status)
        return str(original(key, data, content_type))

    s3.upload_file = upload  # type: ignore[method-assign]
    assert _stage(session_factory, doc_id) is bs.StageOutcome.STAGED

    key = f"{PREFIX}{org_id}/{doc_id}.txt"
    assert s3.puts[key] == b"hello"
    assert json.loads(s3.puts[f"{key}.metadata.json"]) == {
        "metadataAttributes": {
            "organization_id": str(org_id),
            "document_id": str(doc_id),
        }
    }
    # Legacy content under the same stem is removed, by key, with sidecars.
    for ext in ("pdf", "png"):
        legacy = f"{PREFIX}{org_id}/{doc_id}.{ext}"
        assert legacy in s3.deletes
        assert f"{legacy}.metadata.json" in s3.deletes
    assert key not in s3.deletes
    # Upload happened while the row was not yet pending.
    assert seen_status == [None, None]
    doc = _load(session_factory, doc_id)
    assert doc.do_kb_sync_status == PENDING
    assert _state(doc)["stage_token"]
    assert "job_id" not in _state(doc)


def test_stage_skips_documents_without_text_or_deleted(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    no_text = _add(session_factory, text=None)
    deleted = _add(session_factory)
    db = session_factory()
    db.get(Document, deleted).soft_delete()
    db.commit()
    db.close()

    assert _stage(session_factory, no_text) is bs.StageOutcome.SKIPPED
    assert _stage(session_factory, deleted) is bs.StageOutcome.SKIPPED
    assert s3.events == []
    assert _load(session_factory, no_text).do_kb_sync_status is None
    assert _load(session_factory, deleted).do_kb_sync_status is None


def test_stage_upload_failure_is_recorded_not_raised(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    doc_id = _add(session_factory)
    s3.fail_upload = True
    assert _stage(session_factory, doc_id) is bs.StageOutcome.FAILED
    assert _load(session_factory, doc_id).do_kb_sync_status == FAILED


def test_tick_retries_a_failed_staging_upload(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    org_id = uuid4()
    doc_id = _add(session_factory, org_id, text="body")
    s3.fail_upload = True
    _stage(session_factory, doc_id)
    assert _state(_load(session_factory, doc_id))["stage_failures"] == 1
    s3.fail_upload = False
    client = FakeBedrock()

    summary = _tick(session_factory, client, s3)

    doc = _load(session_factory, doc_id)
    assert s3.puts[f"{PREFIX}{org_id}/{doc_id}.txt"] == b"body"
    assert doc.do_kb_sync_status == PENDING
    assert "stage_failures" not in _state(doc)
    # Restaged and claimed by the job started in the same tick.
    assert summary["restaged"] == 1 and _state(doc)["job_id"] == "job-1"


def test_tick_stops_retrying_staging_after_the_attempt_budget(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    doc_id = _add(session_factory)
    s3.fail_upload = True
    _stage(session_factory, doc_id)
    client = FakeBedrock()

    summaries = [_tick(session_factory, client, s3) for _ in range(4)]

    retried = [s.get("stage_retry_failed", 0) for s in summaries]
    assert retried == [1, 1, 0, 0]
    doc = _load(session_factory, doc_id)
    assert doc.do_kb_sync_status == FAILED
    assert _state(doc)["stage_failures"] == bs.MAX_ATTEMPTS
    assert client.started == []


def test_tick_does_not_retry_staging_for_a_deleted_document(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    doc_id = _add(session_factory)
    s3.fail_upload = True
    _stage(session_factory, doc_id)
    s3.fail_upload = False
    db = session_factory()
    db.get(Document, doc_id).is_deleted = True
    db.commit()
    db.close()

    summary = _tick(session_factory, FakeBedrock(), s3)

    assert "restaged" not in summary and s3.puts == {}


def test_stage_failure_keeps_a_concurrent_delete_queued(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    """A soft delete that commits mid-upload must stay pending for removal."""
    doc_id = _add(session_factory)

    def upload(key: str, data: bytes, content_type: str = "") -> str:
        other = session_factory()
        try:
            document = other.get(Document, doc_id)
            document.is_deleted = True
            bs.mark_deleted(document)
            other.commit()
        finally:
            other.close()
        raise RuntimeError("s3 down")

    s3.upload_file = upload  # type: ignore[method-assign]

    assert _stage(session_factory, doc_id) is bs.StageOutcome.FAILED

    doc = _load(session_factory, doc_id)
    assert doc.is_deleted is True
    assert doc.do_kb_sync_status == PENDING
    assert _state(doc).get("stage_token")


def test_restage_clears_an_existing_claim(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    doc_id = _add(session_factory)
    _stage(session_factory, doc_id)
    client = FakeBedrock()
    _tick(session_factory, client, s3)
    assert _state(_load(session_factory, doc_id))["job_id"] == "job-1"

    _stage(session_factory, doc_id)

    doc = _load(session_factory, doc_id)
    assert doc.do_kb_sync_status == PENDING
    assert "job_id" not in _state(doc)


# --------------------------------------------------------------- claim rule


def test_document_staged_after_selection_is_not_claimed(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    selected = _add(session_factory)
    restaged = _add(session_factory)
    late = _add(session_factory)
    _stage(session_factory, selected)
    _stage(session_factory, restaged)
    token_before = _state(_load(session_factory, restaged))["stage_token"]

    client = FakeBedrock()

    def stage_during_start() -> None:
        # Runs after the tick selected its ids and before the job id returns.
        _stage(session_factory, late)
        _stage(session_factory, restaged)

    client.on_start = stage_during_start
    summary = _tick(session_factory, client, s3)

    assert client.started == ["job-1"]
    assert summary["claimed"] == 1
    assert _state(_load(session_factory, selected))["job_id"] == "job-1"
    for doc_id in (late, restaged):
        doc = _load(session_factory, doc_id)
        assert doc.do_kb_sync_status == PENDING
        assert "job_id" not in _state(doc), "job claimed a file it never saw"
    assert _state(_load(session_factory, restaged))["stage_token"] != token_before

    # Both wait for, and get, the next job.
    client.jobs["job-1"]["status"] = "COMPLETE"
    client.on_start = None
    _tick(session_factory, client, s3)
    assert _load(session_factory, selected).do_kb_sync_status == COMPLETED
    assert client.started == ["job-1", "job-2"]
    for doc_id in (late, restaged):
        assert _state(_load(session_factory, doc_id))["job_id"] == "job-2"


# ----------------------------------------------------------- settlement


def test_pending_row_never_staged_here_is_not_claimed(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    """A pending status without a stage token is a DigitalOcean-era leftover
    whose file is not under this prefix; a job must not mark it indexed."""
    leftover = _add(session_factory)
    db = session_factory()
    db.get(Document, leftover).do_kb_sync_status = PENDING
    db.commit()
    db.close()
    staged = _add(session_factory)
    _stage(session_factory, staged)
    client = FakeBedrock()

    summary = _tick(session_factory, client, s3)

    assert summary["claimed"] == 1
    assert "job_id" not in _state(_load(session_factory, leftover))
    assert _state(_load(session_factory, staged))["job_id"] == "job-1"


def test_complete_job_marks_completed_except_reported_failures(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    org_id = uuid4()
    good = _add(session_factory, org_id)
    bad = _add(session_factory, org_id)
    _stage(session_factory, good)
    _stage(session_factory, bad)
    client = FakeBedrock()
    _tick(session_factory, client, s3)
    client.jobs["job-1"].update(
        status="COMPLETE",
        failureReasons=[
            f"Encountered error: s3://bucket/{PREFIX}{org_id}/{bad}.txt is malformed"
        ],
    )

    summary = _tick(session_factory, client, s3)

    done = _load(session_factory, good)
    assert done.do_kb_sync_status == COMPLETED
    assert done.do_kb_indexed_at is not None
    assert "job_id" not in _state(done)
    retry = _load(session_factory, bad)
    assert retry.do_kb_sync_status == PENDING
    assert _state(retry)["attempts"] == 1
    # The retry was re-queued on the next job in the same tick.
    assert summary["completed"] == 1 and summary["retried"] == 1
    assert _state(retry)["job_id"] == "job-2"


def test_complete_job_with_unnamed_failures_completes_nothing(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    """A failed-document count the reasons do not explain proves nothing."""
    first = _add(session_factory)
    second = _add(session_factory)
    _stage(session_factory, first)
    _stage(session_factory, second)
    client = FakeBedrock()
    _tick(session_factory, client, s3)
    client.jobs["job-1"].update(
        status="COMPLETE",
        failureReasons=[],
        statistics={"numberOfDocumentsFailed": 1},
    )

    summary = _tick(session_factory, client, s3)

    assert summary["completed"] == 0 and summary["retried"] == 2
    assert summary["unattributed_failures"] == 1
    for doc_id in (first, second):
        doc = _load(session_factory, doc_id)
        assert doc.do_kb_sync_status == PENDING
        assert _state(doc)["attempts"] == 1


@pytest.mark.parametrize("statistics", [None, {}, {"numberOfDocumentsScanned": 2}])
def test_complete_job_without_a_failed_count_completes_nothing(
    session_factory: Any, s3: Any, enabled: Any, statistics: Any
) -> None:
    """The API may omit the statistics or the count: unknown is not zero."""
    doc_id = _add(session_factory)
    _stage(session_factory, doc_id)
    client = FakeBedrock()
    _tick(session_factory, client, s3)
    client.jobs["job-1"]["status"] = "COMPLETE"
    if statistics is None:
        del client.jobs["job-1"]["statistics"]
    else:
        client.jobs["job-1"]["statistics"] = statistics

    summary = _tick(session_factory, client, s3)

    doc = _load(session_factory, doc_id)
    assert summary["completed"] == 0 and summary["retried"] == 1
    assert summary["unattributed_failures"] == "unknown"
    assert doc.do_kb_sync_status == PENDING and _state(doc)["attempts"] == 1


def test_complete_job_with_every_failure_named_completes_the_rest(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    org_id = uuid4()
    good = _add(session_factory, org_id)
    bad = _add(session_factory, org_id)
    _stage(session_factory, good)
    _stage(session_factory, bad)
    client = FakeBedrock()
    _tick(session_factory, client, s3)
    client.jobs["job-1"].update(
        status="COMPLETE",
        failureReasons=[f"s3://bucket/{PREFIX}{org_id}/{bad}.txt is malformed"],
        statistics={"numberOfDocumentsFailed": 1},
    )

    summary = _tick(session_factory, client, s3)

    assert summary["completed"] == 1 and summary["retried"] == 1
    assert _load(session_factory, good).do_kb_sync_status == COMPLETED


def test_restage_during_settlement_is_not_marked_completed(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    doc_id = _add(session_factory)
    _stage(session_factory, doc_id)
    client = FakeBedrock()
    _tick(session_factory, client, s3)
    client.jobs["job-1"]["status"] = "COMPLETE"
    original_get = client.get_ingestion_job

    def get_then_restage(**kwargs: Any) -> dict[str, Any]:
        # The tick already listed this row as claimed by job-1.
        _stage(session_factory, doc_id)
        return original_get(**kwargs)

    client.get_ingestion_job = get_then_restage  # type: ignore[method-assign]
    client.running_elsewhere = True  # keep this tick from claiming it again
    summary = _tick(session_factory, client, s3)

    doc = _load(session_factory, doc_id)
    assert summary["completed"] == 0
    assert doc.do_kb_sync_status == PENDING, "new content marked indexed by old job"
    assert "job_id" not in _state(doc)


def test_failed_job_retries_then_marks_failed(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    doc_id = _add(session_factory)
    _stage(session_factory, doc_id)
    client = FakeBedrock()
    for attempt in range(1, bs.MAX_ATTEMPTS + 1):
        _tick(session_factory, client, s3)  # claims onto a new job
        job_id = _state(_load(session_factory, doc_id))["job_id"]
        client.jobs[job_id]["status"] = "FAILED" if attempt % 2 else "STOPPED"
        _tick(session_factory, client, s3)  # settles it
        doc = _load(session_factory, doc_id)
        if attempt < bs.MAX_ATTEMPTS:
            assert doc.do_kb_sync_status == PENDING
            assert _state(doc)["attempts"] == attempt
    assert doc.do_kb_sync_status == FAILED
    assert _state(doc)["attempts"] == bs.MAX_ATTEMPTS
    started = len(client.started)
    _tick(session_factory, client, s3)
    assert len(client.started) == started, "a failed row must not start jobs"


def test_running_job_blocks_a_second_start(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    first = _add(session_factory)
    _stage(session_factory, first)
    client = FakeBedrock()
    _tick(session_factory, client, s3)
    second = _add(session_factory)
    _stage(session_factory, second)
    client.jobs["job-1"]["status"] = "IN_PROGRESS"

    summary = _tick(session_factory, client, s3)
    _tick(session_factory, client, s3)

    assert client.started == ["job-1"]
    assert summary["in_flight_job"] == "job-1"
    assert "job_id" not in _state(_load(session_factory, second))


def test_job_started_elsewhere_or_conflict_starts_nothing(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    doc_id = _add(session_factory)
    _stage(session_factory, doc_id)
    client = FakeBedrock()
    client.running_elsewhere = True
    assert _tick(session_factory, client, s3)["in_flight_job"] == "external"
    assert "start" not in client.calls

    client.running_elsewhere = False
    client.conflict = True
    assert _tick(session_factory, client, s3)["conflict"] is True
    doc = _load(session_factory, doc_id)
    assert doc.do_kb_sync_status == PENDING
    assert "job_id" not in _state(doc)


# ------------------------------------------------------------------ deletion


def test_deleted_document_objects_removed_before_job(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    org_id = uuid4()
    doc_id = _add(session_factory, org_id)
    _stage(session_factory, doc_id)
    client = FakeBedrock()
    _tick(session_factory, client, s3)
    client.jobs["job-1"]["status"] = "COMPLETE"
    _tick(session_factory, client, s3)
    assert _load(session_factory, doc_id).do_kb_sync_status == COMPLETED

    db = session_factory()
    doc = db.get(Document, doc_id)
    bs.mark_deleted(doc)  # what soft_delete_documents does in its transaction
    doc.soft_delete()
    db.commit()
    db.close()
    s3.events.clear()
    order: list[str] = []
    client.on_start = lambda: order.append("start")
    original_delete = s3.delete_file

    def delete(key: str) -> bool:
        order.append("delete")
        return bool(original_delete(key))

    s3.delete_file = delete  # type: ignore[method-assign]
    _tick(session_factory, client, s3)

    key = f"{PREFIX}{org_id}/{doc_id}.txt"
    assert key in s3.deletes and f"{key}.metadata.json" in s3.deletes
    assert order.index("start") > max(i for i, e in enumerate(order) if e == "delete")
    assert _state(_load(session_factory, doc_id))["job_id"] == "job-2"
    client.jobs["job-2"]["status"] = "COMPLETE"
    _tick(session_factory, client, s3)
    assert _load(session_factory, doc_id).do_kb_sync_status == COMPLETED


def test_deleted_document_with_failed_object_delete_waits(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    org_id = uuid4()
    doc_id = _add(session_factory, org_id)
    db = session_factory()
    doc = db.get(Document, doc_id)
    bs.mark_deleted(doc)
    doc.soft_delete()
    db.commit()
    db.close()
    s3.fail_delete.add(f"{PREFIX}{org_id}/{doc_id}.txt")
    client = FakeBedrock()

    _tick(session_factory, client, s3)

    assert client.started == []
    doc = _load(session_factory, doc_id)
    assert doc.do_kb_sync_status == PENDING
    assert "job_id" not in _state(doc)


# --------------------------------------------------------------- single runner


def _connection(acquired: bool) -> MagicMock:
    from unittest.mock import AsyncMock

    connection = MagicMock()
    result = MagicMock()
    result.scalar.return_value = acquired
    connection.execute = AsyncMock(return_value=result)
    connection.commit = AsyncMock()
    return connection


async def test_tick_does_nothing_without_the_advisory_lock(enabled: Any) -> None:
    connection = _connection(False)
    session_factory = MagicMock()
    client = MagicMock()

    assert await run_locked_tick(connection, session_factory, client) == {
        "skipped": "locked"
    }

    session_factory.assert_not_called()
    assert client.method_calls == []
    statements = [str(c.args[0]) for c in connection.execute.await_args_list]
    assert statements == ["SELECT pg_try_advisory_lock(:ns, :key)"]


async def test_tick_releases_the_lock_it_took(enabled: Any) -> None:
    from unittest.mock import AsyncMock

    connection = _connection(True)
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    with patch(
        "src.tasks.bedrock_sync_tasks.run_sync_tick",
        AsyncMock(return_value={"claimed": 0}),
    ) as tick:
        result = await run_locked_tick(connection, lambda: db, MagicMock())
    assert result == {"claimed": 0}
    tick.assert_awaited_once()
    db.__aexit__.assert_awaited_once()
    statements = [str(c.args[0]) for c in connection.execute.await_args_list]
    assert statements[-1] == "SELECT pg_advisory_unlock(:ns, :key)"


# ------------------------------------------------------------ delete hook


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_on", [True, False])
async def test_soft_delete_queues_vector_removal_in_its_transaction(
    sync_on: Any,
) -> None:
    from unittest.mock import AsyncMock

    from src.services.documents.file_service import FileService

    doc = Document(
        id=uuid4(),
        organization_id=uuid4(),
        file_size_bytes=10,
        do_kb_sync_status=COMPLETED,
        document_metadata={"other": 1},
    )
    selected = MagicMock()
    selected.scalars.return_value.all.return_value = [doc]
    no_jobs = MagicMock()
    no_jobs.scalars.return_value.all.return_value = []
    svc = object.__new__(FileService)  # skip heavy __init__
    svc.db = AsyncMock()
    svc.db.execute = AsyncMock(side_effect=[selected, no_jobs, None, None])
    svc._revoke_tasks = MagicMock()  # type: ignore[method-assign]
    data_source = "DSID" if sync_on else ""

    with (
        patch.object(bs.settings, "BEDROCK_KB_ID", "KBID"),
        patch.object(bs.settings, "BEDROCK_KB_DATA_SOURCE_ID", data_source),
    ):
        deleted = await svc.soft_delete_documents(doc.organization_id, [doc.id])

    assert deleted == [doc] and doc.is_deleted is True
    svc.db.commit.assert_awaited_once()
    assert doc.document_metadata["other"] == 1
    if sync_on:
        assert doc.do_kb_sync_status == PENDING
        assert _state(doc)["stage_token"]
    else:
        assert doc.do_kb_sync_status == COMPLETED
        assert bs.META_KEY not in doc.document_metadata


def test_arxiv_restages_only_documents_not_yet_indexed(enabled: Any) -> None:
    from src.services.arxiv.persistence import _needs_kb_sync

    # Bedrock records no data-source uuid: indexed means completed by this queue.
    staged = {bs.META_KEY: {"stage_token": "t", "text_sha256": bs._text_sha("x")}}
    assert (
        _needs_kb_sync(
            Document(
                do_kb_sync_status=COMPLETED, document_metadata=staged, content_text="x"
            )
        )
        is False
    )
    # No recorded hash: the upload cannot be shown to match, so restage.
    unhashed = {bs.META_KEY: {"stage_token": "t"}}
    assert (
        _needs_kb_sync(
            Document(
                do_kb_sync_status=COMPLETED,
                document_metadata=unhashed,
                content_text="x",
            )
        )
        is True
    )
    # A completed status left by the retired DigitalOcean KB must restage.
    assert _needs_kb_sync(Document(do_kb_sync_status=COMPLETED)) is True
    assert _needs_kb_sync(Document(do_kb_sync_status=PENDING)) is True
    assert _needs_kb_sync(Document(do_kb_sync_status=None)) is True
    # An indexed document whose full text was just upgraded must restage.
    indexed = Document(do_kb_sync_status=COMPLETED, document_metadata=staged)
    assert _needs_kb_sync(indexed, content_changed=True) is True


def test_arxiv_restages_when_text_differs_from_the_staged_upload(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    """A storage repair rewrites the text without reporting an upgrade."""
    from src.services.arxiv.persistence import _needs_kb_sync

    doc_id = _add(session_factory, text="old")
    _stage(session_factory, doc_id)
    db = session_factory()
    document = db.get(Document, doc_id)
    document.do_kb_sync_status = COMPLETED
    assert _needs_kb_sync(document) is False

    from types import SimpleNamespace

    from src.services.arxiv.persistence import _repair_document

    source = SimpleNamespace(
        title="Paper",
        content_text="repaired",
        content_summary=None,
        document_metadata={"has_full_text": True},
    )
    _repair_document(document, source, "2605.26355v1", {})

    assert document.content_text == "repaired"
    assert _needs_kb_sync(document) is True
    db.close()


def _add_deleted_with_upload_token(session_factory: Any, *, minutes_ago: int) -> Any:
    from datetime import datetime, timedelta, timezone

    from src.services.documents.satellite_state import begin_write

    doc_id = _add(session_factory)
    db = session_factory()
    document = db.get(Document, doc_id)
    document.is_deleted = True
    document.do_kb_sync_status = COMPLETED  # removal already settled
    begin_write(
        document,
        bs.WRITE_SATELLITE,
        "tok",
        now=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    db.commit()
    db.close()
    return doc_id


def test_tick_requeues_removal_after_a_dead_worker_upload(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    """The worker died after uploading; only its durable token says so."""
    from src.services.documents.satellite_state import pending_writes

    doc_id = _add_deleted_with_upload_token(session_factory, minutes_ago=20)
    client = FakeBedrock()

    summary = _tick(session_factory, client, s3)

    doc = _load(session_factory, doc_id)
    assert summary["requeued_orphans"] == 1
    assert pending_writes(doc, bs.WRITE_SATELLITE) == {}
    # Removed again and covered by a job started in the same tick.
    assert any(str(doc_id) in key for key in s3.deletes)
    assert doc.do_kb_sync_status == PENDING and _state(doc)["job_id"] == "job-1"


def test_tick_waits_while_an_upload_may_still_be_running(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    from src.services.documents.satellite_state import pending_writes

    doc_id = _add_deleted_with_upload_token(session_factory, minutes_ago=1)
    client = FakeBedrock()

    summary = _tick(session_factory, client, s3)

    doc = _load(session_factory, doc_id)
    assert "requeued_orphans" not in summary and client.started == []
    assert doc.do_kb_sync_status == COMPLETED
    assert "tok" in pending_writes(doc, bs.WRITE_SATELLITE)


def test_late_upload_for_a_deleted_document_requeues_its_removal(
    session_factory: Any, enabled: Any
) -> None:
    """An upload that lands after cleanup must not leave indexable objects."""
    from src.tasks.processing_tasks import _compensate_late_remote_writes

    org_id = uuid4()
    doc_id = _add(session_factory, org_id)
    db = session_factory()
    document = db.get(Document, doc_id)
    document.is_deleted = True
    document.do_kb_sync_status = COMPLETED  # the tick already settled removal
    token = bs.begin_stage_write(document)
    db.commit()

    # A second writer is uploading for the same document.
    from src.services.documents.satellite_state import begin_write

    begin_write(document, bs.WRITE_SATELLITE, "other-writer")
    db.commit()

    _compensate_late_remote_writes(
        db, doc_id, org_id, {"bedrock_kb"}, bedrock_token=token
    )
    db.close()

    doc = _load(session_factory, doc_id)
    assert doc.do_kb_sync_status == PENDING
    assert _state(doc)["stage_token"] and "job_id" not in _state(doc)
    # The handler ran after the upload, so its token is settled here.
    from src.services.documents.satellite_state import pending_writes

    assert token not in pending_writes(doc, bs.WRITE_SATELLITE)
    # The other writer's token survives, so its upload is still cleaned up.
    assert "other-writer" in pending_writes(doc, bs.WRITE_SATELLITE)


def test_pipeline_stages_the_final_text_after_results_are_stored(
    session_factory: Any, s3: Any, enabled: Any
) -> None:
    from src.services.processing.multimodal_processing_service import (
        MultimodalProcessingService,
    )

    org_id = uuid4()
    doc_id = _add(session_factory, org_id, text="old")
    db = session_factory()
    document = db.get(Document, doc_id)
    pipeline = MultimodalProcessingService.__new__(MultimodalProcessingService)
    pipeline.db = db
    results: dict[str, Any] = {
        "success": True,
        "errors": [],
        "text_extraction": {"text_content": "new"},
    }

    asyncio.run(pipeline.update_document_with_results(document, results))
    pipeline._stage_for_bedrock(document, results)
    db.close()

    assert s3.puts[f"{PREFIX}{org_id}/{doc_id}.txt"] == b"new"
    assert results["bedrock_kb_stage"] == "staged"
    assert _load(session_factory, doc_id).do_kb_sync_status == PENDING


@pytest.mark.asyncio
@pytest.mark.parametrize("async_stage", [False, True])
async def test_stage_preserves_other_writer_token(
    session_factory: Any,
    enabled: Any,
    monkeypatch: pytest.MonkeyPatch,
    async_stage: bool,
) -> None:
    """Remove the post-upload locked refresh and the second token is lost.

    Focus: pytest -q backend/tests/unit/services/test_bedrock_sync.py -k preserves_other_writer
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.services.documents.satellite_state import pending_writes

    doc_id = _add(session_factory)
    with session_factory() as db:
        token = bs.begin_stage_write(db.get(Document, doc_id))
        db.commit()

    other_token = None

    def upload(*args: Any) -> None:
        nonlocal other_token
        with session_factory() as other:
            other_token = bs.begin_stage_write(other.get(Document, doc_id))
            other.commit()
        visible = pending_writes(_load(session_factory, doc_id), bs.WRITE_SATELLITE)
        assert set(visible) == {token, other_token}

    monkeypatch.setattr(bs, "upload_document_objects", upload)
    if async_stage:
        url = session_factory.kw["bind"].url.set(drivername="sqlite+aiosqlite")
        engine = create_async_engine(url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                doc = await db.get(Document, doc_id)
                assert await bs.stage_document(db, doc) is bs.StageOutcome.STAGED
                bs.finish_stage_write(doc, token)
                await db.commit()
        finally:
            await engine.dispose()
    else:
        with session_factory() as db:
            doc = db.get(Document, doc_id)
            assert bs.stage_document_sync(db, doc) is bs.StageOutcome.STAGED
            bs.finish_stage_write(doc, token)
            db.commit()

    assert other_token is not None
    assert set(pending_writes(_load(session_factory, doc_id), bs.WRITE_SATELLITE)) == {
        other_token
    }


@pytest.mark.asyncio
async def test_prune_only_deleted_and_missing_staged_objects(
    session_factory: Any, enabled: Any, s3: Any
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    org = uuid4()
    live = _add(session_factory, org)
    deleted = _add(session_factory, org)
    with session_factory() as db:
        db.get(Document, deleted).soft_delete()
        db.commit()

    deleted_key = bs.content_key(org, deleted, "pdf")
    missing_sidecar = bs.sidecar_key(bs.content_key(org, uuid4()))
    s3.list_objects = MagicMock(
        side_effect=[
            [
                bs.content_key(org, live),
                deleted_key,
                bs.sidecar_key(deleted_key),
                missing_sidecar,
                f"{PREFIX}{org}/{uuid4()}.csv",
                f"unrelated/{org}/{uuid4()}.txt",
            ],
            [
                bs.content_key(org, live),
                deleted_key,
                bs.sidecar_key(deleted_key),
                missing_sidecar,
            ],
            [bs.content_key(org, live)],
        ]
    )
    engine = create_async_engine(
        session_factory.kw["bind"].url.set(drivername="sqlite+aiosqlite")
    )
    client = FakeBedrock()
    try:
        async with async_sessionmaker(engine)() as db:
            assert await bs.prune_staged_objects(db, client, s3, apply=False) == {
                "stale_objects": 3
            }
            assert not s3.deletes and not client.calls
            assert await bs.prune_staged_objects(db, client, s3, apply=True) == {
                "stale_objects": 3,
                "started_job": "job-1",
            }
            assert s3.deletes == [
                deleted_key,
                bs.sidecar_key(deleted_key),
                missing_sidecar,
            ]
            assert await bs.prune_staged_objects(db, client, s3, apply=True) == {
                "stale_objects": 0,
                "started_job": "job-2",
            }
            assert client.calls == ["list", "start", "list", "start"]
    finally:
        await engine.dispose()
