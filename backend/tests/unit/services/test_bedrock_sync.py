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
        self.jobs[job_id] = {"ingestionJobId": job_id, "status": "STARTING"}
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
        return original(key, data, content_type)

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
        return original_delete(key)

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
    staged = {bs.META_KEY: {"stage_token": "t"}}
    assert (
        _needs_kb_sync(Document(do_kb_sync_status=COMPLETED, document_metadata=staged))
        is False
    )
    # A completed status left by the retired DigitalOcean KB must restage.
    assert _needs_kb_sync(Document(do_kb_sync_status=COMPLETED)) is True
    assert _needs_kb_sync(Document(do_kb_sync_status=PENDING)) is True
    assert _needs_kb_sync(Document(do_kb_sync_status=None)) is True
