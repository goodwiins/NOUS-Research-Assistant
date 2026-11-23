"""``load_document_pins`` on the non-PostgreSQL path (SQLite), run in CI.

The PostgreSQL branch hashes in SQL and is covered by the research Postgres
suites; these pin the contract both branches share: the checksum wins only
when it is a real SHA-256 (lowercased), otherwise the text hash stands in,
NULL text hashes as ``''``, and only the requested ids come back.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from typing import Optional

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.document import Document, DocumentType
from src.services.research import extraction_forms_service as svc

pytestmark = pytest.mark.unit


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Document.__table__.create(sync))
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _doc(
    db: AsyncSession, content: Optional[str], checksum: Optional[str]
) -> uuid.UUID:
    doc_id = uuid.uuid4()
    db.add(
        Document(
            id=doc_id,
            title="t",
            filename="f.txt",
            file_path="/f.txt",
            file_size_bytes=1,
            mime_type="text/plain",
            document_type=DocumentType.TEXT,
            organization_id=uuid.uuid4(),
            content_text=content,
            checksum_sha256=checksum,
        )
    )
    await db.flush()
    return doc_id


@pytest.mark.asyncio
async def test_valid_checksum_is_lowercased_and_text_hash_kept(
    db: AsyncSession,
) -> None:
    file_sha = _sha("the uploaded file bytes")
    doc = await _doc(db, "Café ünïcode", file_sha.upper())
    pins = await svc.load_document_pins(db, [doc])
    assert pins == {doc: (file_sha, _sha("Café ünïcode"))}


@pytest.mark.asyncio
@pytest.mark.parametrize("checksum", [None, "md5:abc", "z" * 64, _sha("x")[:63]])
async def test_non_sha256_checksum_falls_back_to_text_hash(
    db: AsyncSession, checksum: Optional[str]
) -> None:
    doc = await _doc(db, "body", checksum)
    pins = await svc.load_document_pins(db, [doc])
    assert pins == {doc: (_sha("body"), _sha("body"))}


@pytest.mark.asyncio
async def test_null_text_hashes_as_empty_string(db: AsyncSession) -> None:
    doc = await _doc(db, None, None)
    pins = await svc.load_document_pins(db, [doc])
    assert pins == {doc: (_sha(""), _sha(""))}


@pytest.mark.asyncio
async def test_only_requested_ids_are_returned(db: AsyncSession) -> None:
    wanted = await _doc(db, "a", None)
    other = await _doc(db, "b", None)
    pins = await svc.load_document_pins(db, iter([wanted]))
    assert set(pins) == {wanted}
    assert other not in pins


@pytest.mark.asyncio
async def test_no_ids_returns_empty_without_querying(db: AsyncSession) -> None:
    await _doc(db, "a", None)
    assert await svc.load_document_pins(db, []) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "checksum"),
    [("x", _sha("f").upper()), ("y", None), (None, "bad"), (None, None)],
)
async def test_matches_document_pins_on_the_loaded_row(
    db: AsyncSession, content: Optional[str], checksum: Optional[str]
) -> None:
    # The batched path must agree with the per-row helper the anchors use.
    doc = await _doc(db, content, checksum)
    pins = await svc.load_document_pins(db, [doc])
    row = await db.get(Document, doc)
    assert pins[doc] == svc.document_pins(row)
