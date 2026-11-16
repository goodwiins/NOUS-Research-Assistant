"""GOO-463: a failed task-id stamp must not break the reprocess responses.

``reprocess_document`` and ``reprocess_file`` commit the job, publish its task,
then stamp the dispatched task id (best effort) and build their response from
the ``document`` they loaded. The stamp runs on its own session, so a stamp
that fails after its transaction began must not expire that ``document``:
reading it would be an implicit async lazy load and turn an already committed
and published job into an HTTP 500 (documents) or 400 (files).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.processing import JobStatus, ProcessingJob

pytestmark = pytest.mark.unit

ORG_ID = uuid.uuid4()
USER_ID = uuid.uuid4()


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    # File-backed so the stamp's own session (a second connection) sees the
    # same database as the producer's session.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reprocess.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Document.__table__.create)
        await conn.run_sync(ProcessingJob.__table__.create)
        # Fail the stamp's UPDATE inside the database, after its transaction
        # began. The endpoints only INSERT the job, so nothing else trips it.
        await conn.execute(
            text(
                "CREATE TRIGGER fail_stamp BEFORE UPDATE ON processing_jobs "
                "BEGIN SELECT RAISE(ABORT, 'stamp failed'); END"
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed_document(db: AsyncSession) -> Document:
    document = Document(
        id=uuid.uuid4(),
        title="Paper",
        filename="paper.pdf",
        file_path="org/paper.pdf",
        file_size_bytes=10,
        mime_type="application/pdf",
        document_type=DocumentType.PDF,
        organization_id=ORG_ID,
        processing_status=ProcessingStatus.FAILED,
    )
    db.add(document)
    await db.commit()
    return document


async def _call(
    endpoint: Any, db: AsyncSession, document: Document, **kwargs: Any
) -> Any:
    """Run ``endpoint`` with the document lock and the broker stubbed."""

    async def lock_document(*_args: Any, **_kwargs: Any) -> Document:
        return document

    with (
        patch(
            "src.services.documents.file_service.FileService."
            "lock_document_for_reprocessing",
            side_effect=lock_document,
        ),
        patch(
            "src.tasks.processing_tasks.process_document_ingestion.delay",
            return_value=MagicMock(id="published-task-id"),
        ) as delay,
    ):
        response = await endpoint(
            current_user=SimpleNamespace(id=USER_ID),
            organization=SimpleNamespace(id=ORG_ID),
            db=db,
            **kwargs,
        )
    delay.assert_called_once()
    return response


async def _only_job(db: AsyncSession) -> ProcessingJob:
    job: ProcessingJob = (await db.execute(select(ProcessingJob))).scalar_one()
    return job


async def test_reprocess_document_responds_when_the_stamp_fails(
    db: AsyncSession,
) -> None:
    from src.api.documents.documents import reprocess_document

    document = await _seed_document(db)

    response = await _call(
        reprocess_document,
        db,
        document,
        document_id=str(document.id),
        force_reprocess=False,
    )

    job = await _only_job(db)
    assert response == {
        "message": "Document queued for reprocessing",
        "document_id": str(document.id),
        "processing_status": "pending",
        "job_id": str(job.id),
    }
    # The stamp did fail: the committed, published job keeps a NULL task id.
    assert job.status is JobStatus.PENDING
    assert job.celery_task_id is None


async def test_reprocess_file_responds_when_the_stamp_fails(db: AsyncSession) -> None:
    from src.api.documents.files import reprocess_file

    document = await _seed_document(db)

    response = await _call(reprocess_file, db, document, file_id=document.id)

    job = await _only_job(db)
    assert response == {
        "message": "File queued for reprocessing",
        "processing_status": "pending",
        "job_id": str(job.id),
    }
    assert job.status is JobStatus.PENDING
    assert job.celery_task_id is None
