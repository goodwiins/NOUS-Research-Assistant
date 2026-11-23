"""GOO-765: the document list types "processed with warnings".

``DocumentResponse.processing_partial`` is derived from the
``processing_partial`` metadata flag the processing pipeline writes on every
run (GOO-658, surfaced on the detail page by GOO-744). It is true only for an
indexed document whose flag is a literal ``True``; anything else is false.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.documents import documents as documents_mod
from src.core.database import get_db
from src.core.dependencies import get_current_organization, get_current_user
from src.models.document import Document, DocumentType, ProcessingStatus

pytestmark = pytest.mark.unit

_ORG_ID = uuid.uuid4()

_PARTIAL = {"processing_partial": True, "processing_warnings": ["OCR failed"]}


def _document(status: ProcessingStatus, metadata: Any) -> Document:
    now = datetime(2026, 11, 1, 12, 0, 0)
    return Document(
        id=uuid.uuid4(),
        title="Report",
        filename="report.pdf",
        document_type=DocumentType.PDF,
        file_size_bytes=2048,
        mime_type="application/pdf",
        processing_status=status,
        tags=[],
        is_public=False,
        content_text="body",
        document_metadata=metadata,
        created_at=now,
        updated_at=now,
        uploaded_by_user_id=uuid.uuid4(),
        organization_id=_ORG_ID,
        processing_retry_count=0,
    )


# (case id, db status, metadata, expected processing_partial)
_CASES = [
    ("partial", ProcessingStatus.COMPLETED, _PARTIAL, True),
    (
        "complete",
        ProcessingStatus.COMPLETED,
        {"processing_partial": False, "processing_warnings": []},
        False,
    ),
    # The pipeline never writes partial on failure, but a stale flag from an
    # earlier run must not make a failed document read as partial.
    ("failed", ProcessingStatus.FAILED, _PARTIAL, False),
    ("failed-clean", ProcessingStatus.FAILED, {"processing_partial": False}, False),
    ("queued-stale", ProcessingStatus.PENDING, _PARTIAL, False),
    ("processing-stale", ProcessingStatus.PROCESSING, _PARTIAL, False),
    ("retrying-stale", ProcessingStatus.RETRYING, _PARTIAL, False),
    ("missing-metadata", ProcessingStatus.COMPLETED, None, False),
    ("empty-metadata", ProcessingStatus.COMPLETED, {}, False),
    ("missing-key", ProcessingStatus.COMPLETED, {"processed_at": "x"}, False),
    (
        "truthy-string",
        ProcessingStatus.COMPLETED,
        {"processing_partial": "true"},
        False,
    ),
    ("truthy-int", ProcessingStatus.COMPLETED, {"processing_partial": 1}, False),
    ("non-dict-metadata", ProcessingStatus.COMPLETED, ["processing_partial"], False),
]


@pytest.mark.parametrize(
    ("status", "metadata", "expected"),
    [case[1:] for case in _CASES],
    ids=[case[0] for case in _CASES],
)
def test_document_processing_partial(
    status: ProcessingStatus, metadata: Any, expected: bool
) -> None:
    document = _document(status, metadata)
    assert documents_mod.document_processing_partial(document) is expected


def test_field_defaults_to_false() -> None:
    """Absent means false: a response built without it never claims partial."""
    field = documents_mod.DocumentResponse.model_fields["processing_partial"]
    assert field.default is False
    assert not field.is_required()


@pytest.fixture()
def db() -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    return session


@pytest.fixture()
def client(db: MagicMock) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(documents_mod.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: MagicMock()
    app.dependency_overrides[get_current_organization] = lambda: MagicMock(id=_ORG_ID)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client


def _page(db: MagicMock, documents: list[Document]) -> None:
    result = MagicMock()
    result.scalar.return_value = len(documents)
    result.scalars.return_value.all.return_value = documents
    db.execute.return_value = result


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/api/v1/documents"), ("post", "/api/v1/documents/search?query=r")],
)
def test_list_and_search_type_processing_partial(
    client: TestClient, db: MagicMock, method: str, path: str
) -> None:
    documents = [_document(case[1], case[2]) for case in _CASES]
    _page(db, documents)

    response = client.request(method, path)

    assert response.status_code == 200, response.text
    rows = response.json()["documents"]
    assert [row["processing_partial"] for row in rows] == [case[3] for case in _CASES]
    # The field and the status agree: partial only on an indexed row.
    for row in rows:
        if row["processing_partial"]:
            assert row["processing_status"] == "indexed"


# The detail response also returns ``metadata`` typed as a dict, so a
# non-dict value fails there for reasons unrelated to this field.
_DETAIL_CASES = [case for case in _CASES if not isinstance(case[2], list)]


@pytest.mark.parametrize(
    ("status", "metadata", "expected"),
    [case[1:] for case in _DETAIL_CASES],
    ids=[case[0] for case in _DETAIL_CASES],
)
def test_detail_matches_list(
    client: TestClient,
    db: MagicMock,
    status: ProcessingStatus,
    metadata: Any,
    expected: bool,
) -> None:
    document = _document(status, metadata)
    result = MagicMock()
    result.scalars.return_value.first.return_value = document
    db.execute.return_value = result

    response = client.get(f"/api/v1/documents/{document.id}")

    assert response.status_code == 200, response.text
    assert response.json()["processing_partial"] is expected


@pytest.mark.parametrize(
    ("status", "metadata", "expected"),
    [case[1:] for case in _CASES],
    ids=[case[0] for case in _CASES],
)
def test_duplicate_check_types_processing_partial(
    client: TestClient,
    db: MagicMock,
    status: ProcessingStatus,
    metadata: Any,
    expected: bool,
) -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = _document(status, metadata)
    db.execute.return_value = result

    response = client.post(
        "/api/v1/documents/check-duplicate", json={"sha256": "a" * 64}
    )

    assert response.status_code == 200, response.text
    assert response.json()["document"]["processing_partial"] is expected
