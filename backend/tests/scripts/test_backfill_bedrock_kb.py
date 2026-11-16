"""``scripts/backfill_bedrock_kb.py``: dry run by default, counts only."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from src.models.document import Document, DocumentType, ProcessingStatus
from src.services.bedrock_sync import StageOutcome
from src.shared.enums import SatelliteSyncStatus

pytestmark = pytest.mark.unit


@pytest.fixture
def session_factory() -> Any:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import src.models  # noqa: F401  register every table on Base.metadata
    from src.models.base import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _seed(
    session_factory: Any,
    org_id: Any,
    *,
    text: Any = "body",
    status: Any = None,
    deleted: Any = False,
    metadata: Any = None,
) -> Any:
    db = session_factory()
    doc = Document(
        id=uuid4(),
        title="Secret title",
        filename="doc.txt",
        file_path="local:///tmp/doc.txt",
        file_size_bytes=10,
        mime_type="text/plain",
        document_type=DocumentType.TEXT,
        organization_id=org_id,
        content_text=text,
        processing_status=ProcessingStatus.COMPLETED,
        do_kb_sync_status=status,
        document_metadata=metadata,
    )
    if deleted:
        doc.soft_delete()
    db.add(doc)
    db.commit()
    db.close()
    return doc.id


def _world(session_factory: Any) -> Any:
    org_a, org_b = uuid4(), uuid4()
    eligible = {
        _seed(session_factory, org_a),
        _seed(session_factory, org_a, status=SatelliteSyncStatus.FAILED.value),
        _seed(session_factory, org_b, status=SatelliteSyncStatus.PENDING.value),
    }
    # A completed status left by the retired DigitalOcean KB is not indexed here.
    eligible.add(
        _seed(session_factory, org_a, status=SatelliteSyncStatus.COMPLETED.value)
    )
    _seed(
        session_factory,
        org_a,
        status=SatelliteSyncStatus.COMPLETED.value,
        metadata={"bedrock_sync": {"stage_token": "t"}},
    )
    _seed(session_factory, org_a, text=None)
    _seed(session_factory, org_b, text="")
    _seed(session_factory, org_b, deleted=True)
    return eligible


def test_dry_run_counts_without_staging(session_factory: Any) -> None:
    from scripts.backfill_bedrock_kb import backfill

    _world(session_factory)
    lines: list[str] = []

    def stage(db: Any, document: Any) -> StageOutcome:  # pragma: no cover
        raise AssertionError("dry run staged a document")

    reports = backfill(session_factory, apply=False, stage=stage, out=lines.append)

    assert sorted(r.eligible for r in reports) == [1, 3]
    assert lines[-1] == "[dry-run] orgs=2 eligible=4 staged=0 skipped=0 failed=0"
    assert not any("Secret title" in line or "body" in line for line in lines)


def test_apply_stages_only_eligible_documents(session_factory: Any) -> None:
    from scripts.backfill_bedrock_kb import backfill

    eligible = _world(session_factory)
    staged: list = []

    def stage(db: Any, document: Any) -> Any:
        staged.append(document.id)
        return StageOutcome.STAGED

    lines: list[str] = []
    backfill(session_factory, apply=True, stage=stage, out=lines.append)

    assert set(staged) == eligible
    assert lines[-1] == "[apply] orgs=2 eligible=4 staged=4 skipped=0 failed=0"
