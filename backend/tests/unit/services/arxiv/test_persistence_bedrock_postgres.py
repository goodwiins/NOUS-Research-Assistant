"""Real-Postgres proof that a soft delete mid-batch cannot abort arXiv staging.

``persist_arxiv_documents`` loads the batch once, then locks and refreshes
each row before staging it. A row soft-deleted between those two steps is
skipped. Ending that skip with ``rollback()`` expired every loaded document.
The next row was then refreshed for only ``STAGE_WRITE_COLUMNS``, leaving
``content_text`` expired; a synchronous read of it in ``stage_document()``
raises ``MissingGreenlet`` and aborts the rest of the batch. Today the
pre-upload commit happens to reload the row (the flush needs the expired
primary key), which masks the crash. The skip must not expire the batch at
all, so the test checks that directly as well as the staged outcome.
Mocks cannot reproduce attribute expiry, so this needs a real session.

Needs ``ORCHESTRATION_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import src.models  # noqa: F401  register every table on Base.metadata
from src.core.config import settings
from src.models.base import Base
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.organization import Organization
from src.models.user import User
from src.services.arxiv.persistence import persist_arxiv_documents
from src.services.bedrock_sync import is_indexed

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

ARXIV_IDS = ("2401.00001v1", "2401.00002v1")


def _dsn() -> str:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg2://" + dsn[len(prefix) :]
    raise ValueError("ORCHESTRATION_TEST_DATABASE_URL must be a PostgreSQL URL")


def _source(arxiv_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        title="Paper",
        content_text="paper content",
        content_summary=None,
        document_metadata={
            "arxiv_id": arxiv_id,
            "title": "Paper",
            "publication_date": datetime(2024, 1, 1, tzinfo=timezone.utc),
        },
    )


@contextmanager
def _two_reusable_papers() -> Iterator[SimpleNamespace]:
    """Isolated schema with two durable arXiv rows still awaiting Bedrock."""
    schema = "arxiv_stage_" + uuid.uuid4().hex
    admin = create_engine(_dsn())
    with admin.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(_dsn(), connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(
            engine,
            tables=[Organization.__table__, User.__table__, Document.__table__],
        )
        factory = sessionmaker(engine, autocommit=False, autoflush=False)
        org_id = uuid.uuid4()
        doc_ids = [uuid.uuid4() for _ in ARXIV_IDS]
        with factory() as db:
            db.add(Organization(id=org_id, name="Org", storage_limit_bytes=10_000))
            db.flush()
            for doc_id, arxiv_id in zip(doc_ids, ARXIV_IDS):
                # Durable, searchable, canonical: the exact-replay path reuses
                # it untouched and only queues it for Bedrock staging.
                db.add(
                    Document(
                        id=doc_id,
                        arxiv_id=arxiv_id,
                        title="Paper",
                        filename=f"{arxiv_id}.pdf",
                        file_path=f"s3://bucket/{arxiv_id}.pdf",
                        file_size_bytes=12,
                        mime_type="application/pdf",
                        document_type=DocumentType.PDF,
                        storage_backend="s3",
                        storage_path=f"{arxiv_id}.pdf",
                        checksum_sha256=f"sha-{arxiv_id}",
                        content_text="paper content",
                        document_metadata={"arxiv_id": arxiv_id},
                        search_vector="paper",
                        processing_status=ProcessingStatus.COMPLETED,
                        organization_id=org_id,
                        do_kb_sync_status="pending",
                    )
                )
            db.commit()
        async_engine = create_async_engine(
            _dsn().replace("+psycopg2", "+asyncpg"),
            connect_args={"server_settings": {"search_path": schema}},
            poolclass=NullPool,
        )
        yield SimpleNamespace(
            Session=factory,
            AsyncSession=async_sessionmaker(async_engine, expire_on_commit=False),
            org_id=org_id,
            doc_ids=doc_ids,
        )
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


async def test_row_deleted_before_its_lock_does_not_abort_the_batch() -> None:
    with _two_reusable_papers() as env:
        deleted: list[uuid.UUID] = []
        next_row_expired: list[str] = []
        real_refresh = AsyncSession.refresh

        async def refresh(self: AsyncSession, instance: Any, **kwargs: Any) -> None:
            if kwargs.get("with_for_update"):
                state = inspect(instance)
                if not deleted:
                    # The batch's first row: soft-delete it from another
                    # session before its lock, as the API would mid-batch.
                    deleted.append(instance.id)
                    with env.Session() as other:
                        other.get(Document, instance.id).is_deleted = True
                        other.commit()
                elif state.identity[0] != deleted[0] and not next_row_expired:
                    # The next row's lock, right after the skip.
                    next_row_expired.extend(sorted(state.expired_attributes))
            await real_refresh(self, instance, **kwargs)

        helper = MagicMock()
        helper.upload_file.return_value = True
        helper.delete_file.return_value = True

        with (
            patch("src.core.database.AsyncSessionLocal", env.AsyncSession),
            patch.object(settings, "BEDROCK_KB_ID", "kb-1"),
            patch.object(settings, "BEDROCK_KB_DATA_SOURCE_ID", "ds-1"),
            patch("src.core.s3_client.S3StorageHelper", return_value=helper),
            patch.object(AsyncSession, "refresh", refresh),
        ):
            result = await persist_arxiv_documents(
                [_source(arxiv_id) for arxiv_id in ARXIV_IDS],
                user_id=None,
                organization_id=env.org_id,
            )

        assert len(deleted) == 1
        (live_id,) = [doc_id for doc_id in env.doc_ids if doc_id != deleted[0]]
        assert next_row_expired == [], "skipping the deleted row expired the batch"
        assert result.kb_sync_failed is False

        uploaded_keys = [call.args[0] for call in helper.upload_file.call_args_list]
        assert any(str(live_id) in key for key in uploaded_keys)
        assert not any(str(deleted[0]) in key for key in uploaded_keys)

        with env.Session() as db:
            live = db.get(Document, live_id)
            assert live.do_kb_sync_status == "pending"
            assert not is_indexed(live.do_kb_sync_status, live.document_metadata)
            assert (live.document_metadata or {}).get(
                "bedrock_sync"
            ), "live row was never staged"
