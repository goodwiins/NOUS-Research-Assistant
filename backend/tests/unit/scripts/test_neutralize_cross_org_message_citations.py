"""GOO-671 backfill: unlink message citations that point at another org's document.

Before #2195, ``POST /api/v2/messages`` stored ``citations[].document_id`` from
the request unchecked, and the presenter falls back to the linked document's
title and type, so those old rows still show a foreign organization's document
whenever the thread is reopened. The one-off script must: default to a dry run
that writes nothing, unlink only citations whose document belongs to a
different organization than the thread (legacy NULL-org workspaces resolve to
their owner's org), clear title/type copied from the foreign document, leave
undeterminable rows alone, and be a no-op on rerun.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Column, MetaData, Table, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scripts.neutralize_cross_org_message_citations import neutralize
from src.models.base import Base
from src.models.chat_message import ChatMessage
from src.models.citation import Citation
from src.models.conversation import Conversation
from src.models.document import Document, DocumentType
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace

pytestmark = pytest.mark.unit

# ``users`` carries encrypted columns that need a key SQLite tests don't set
# up. The script reads only users.id / users.organization_id, so a stand-in
# table with exactly those columns (same types) is enough.
_USERS = Table(
    "users",
    MetaData(),
    Column("id", User.__table__.c.id.type, primary_key=True),
    Column("organization_id", User.__table__.c.organization_id.type),
)

_TABLES = [
    Workspace.__table__,
    Conversation.__table__,
    Thread.__table__,
    ChatMessage.__table__,
    Document.__table__,
    Citation.__table__,
]


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=_TABLES)
        )
        await connection.run_sync(_USERS.create)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class _World:
    """Seed helper: each call inserts the minimal rows and returns ids."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def user(self, org: Optional[UUID]) -> UUID:
        user_id = uuid4()
        await self.db.execute(insert(_USERS).values(id=user_id, organization_id=org))
        return user_id

    async def message(self, owner: UUID, workspace_org: Optional[UUID]) -> UUID:
        ws, conv, thread, msg = uuid4(), uuid4(), uuid4(), uuid4()
        await self.db.execute(
            insert(Workspace.__table__).values(
                id=ws, name="w", owner_id=owner, organization_id=workspace_org
            )
        )
        await self.db.execute(
            insert(Conversation.__table__).values(
                id=conv, workspace_id=ws, title="c", created_by_id=owner
            )
        )
        await self.db.execute(
            insert(Thread.__table__).values(
                id=thread, conversation_id=conv, created_by_id=owner
            )
        )
        await self.db.execute(
            insert(ChatMessage.__table__).values(
                id=msg, thread_id=thread, role="assistant", content="a"
            )
        )
        return msg

    async def document(self, org: UUID, title: str) -> UUID:
        doc = uuid4()
        await self.db.execute(
            insert(Document.__table__).values(
                id=doc,
                title=title,
                filename="f.pdf",
                file_path="s3://b/f.pdf",
                file_size_bytes=1,
                mime_type="application/pdf",
                document_type=DocumentType.PDF,
                organization_id=org,
            )
        )
        return doc

    async def citation(
        self, msg: Optional[UUID], doc: Optional[UUID], **fields: Any
    ) -> UUID:
        cit = uuid4()
        await self.db.execute(
            insert(Citation.__table__).values(
                id=cit, message_id=msg, document_id=doc, **fields
            )
        )
        return cit


async def _seed(factory: async_sessionmaker[AsyncSession]) -> Dict[str, UUID]:
    org_a, org_b = uuid4(), uuid4()
    async with factory() as db:
        w = _World(db)
        owner_a = await w.user(org_a)
        orphan_owner = await w.user(None)
        msg = await w.message(owner_a, org_a)
        legacy_msg = await w.message(owner_a, None)  # NULL-org workspace -> owner's org
        unknown_msg = await w.message(orphan_owner, None)  # org undeterminable
        own_doc = await w.document(org_a, "ours")
        foreign_doc = await w.document(org_b, "SECRET foreign title")
        ids = {
            "own": await w.citation(msg, own_doc, chunk_index=0),
            "foreign": await w.citation(
                msg,
                foreign_doc,
                chunk_index=1,
                snippet="client snippet",
                document_title="SECRET foreign title",
                document_type="pdf",
            ),
            "foreign_own_title": await w.citation(
                msg, foreign_doc, chunk_index=2, document_title="client label"
            ),
            "legacy_own": await w.citation(legacy_msg, own_doc, chunk_index=3),
            "legacy_foreign": await w.citation(legacy_msg, foreign_doc, chunk_index=4),
            "unknown": await w.citation(unknown_msg, foreign_doc, chunk_index=5),
            "external": await w.citation(
                msg, None, chunk_index=6, external_reference_id="arxiv:1"
            ),
            "standalone": await w.citation(None, foreign_doc, chunk_index=7),
        }
        await db.commit()
    return ids


async def _rows(factory: async_sessionmaker[AsyncSession]) -> Dict[UUID, Any]:
    async with factory() as db:
        result = await db.execute(
            select(
                Citation.id,
                Citation.document_id,
                Citation.document_title,
                Citation.document_type,
                Citation.snippet,
            )
        )
        return {row.id: row for row in result}


@pytest.mark.asyncio
async def test_dry_run_reports_counts_and_writes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(factory)
    before = await _rows(factory)

    result = await neutralize(factory, apply=False)

    assert result == {
        "scanned": 6,
        "cross_org": 3,
        "unlinked": 0,
        "copied_fields_cleared": 0,
        "undetermined_org": 1,
    }
    assert await _rows(factory) == before


@pytest.mark.asyncio
async def test_apply_unlinks_only_cross_org_and_rerun_is_a_noop(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(factory)
    before = await _rows(factory)

    first = await neutralize(factory, apply=True)
    assert first == {
        "scanned": 6,
        "cross_org": 3,
        "unlinked": 3,
        "copied_fields_cleared": 1,
        "undetermined_org": 1,
    }

    after = await _rows(factory)
    for key in ("foreign", "foreign_own_title", "legacy_foreign"):
        assert after[ids[key]].document_id is None, key
    # Values copied from the foreign document are cleared; client text stays.
    assert after[ids["foreign"]].document_title is None
    assert after[ids["foreign"]].document_type is None
    assert after[ids["foreign"]].snippet == "client snippet"
    assert after[ids["foreign_own_title"]].document_title == "client label"
    # Same-org, undeterminable, external and standalone rows are untouched.
    for key in ("own", "legacy_own", "unknown", "external", "standalone"):
        assert after[ids[key]] == before[ids[key]], key

    second = await neutralize(factory, apply=True)
    assert second == {
        "scanned": 3,
        "cross_org": 0,
        "unlinked": 0,
        "copied_fields_cleared": 0,
        "undetermined_org": 1,
    }
    assert await _rows(factory) == after


@pytest.mark.asyncio
async def test_small_batches_give_the_same_result(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(factory)
    result = await neutralize(factory, apply=True, batch_size=1)
    assert result["unlinked"] == 3
    assert (await neutralize(factory, apply=True, batch_size=1))["cross_org"] == 0
