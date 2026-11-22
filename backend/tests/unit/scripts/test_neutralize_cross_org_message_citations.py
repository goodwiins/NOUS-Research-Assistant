"""GOO-671 backfill: unlink message citations whose document the thread may not show.

Before #2195, ``POST /api/v2/messages`` stored ``citations[].document_id`` from
the request unchecked, and the presenter falls back to the linked document's
title and type, so old rows can still show another organization's document
whenever the thread is reopened. #2195 keeps a citation's document only when
it belongs to the *citing user's* organization and is not soft-deleted.

The one-off script must match that as closely as stored data allows:

* User-authored messages: exact parity, so the document must be in the
  author's org (case B: the thread's own org is not enough).
* Assistant/system messages have no author, so the document may be in any org
  that legitimately takes part in the thread: workspace org, owner org, thread
  creator org, or an active member's org (case A: a cross-org member's own
  document is kept). A removed member's org does not count.
* Soft-deleted documents are unlinked, but their copied title stays (it is the
  thread's own org's data).
* Dry run by default, writes nothing, but reports would_* counts; every
  affected row is emitted as one NDJSON audit record in both modes, with no
  titles; the concurrent-edit guard holds; a rerun is a no-op.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Column, MetaData, Table, insert, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import scripts.neutralize_cross_org_message_citations as script
from scripts.neutralize_cross_org_message_citations import neutralize
from src.models.base import Base
from src.models.chat_message import ChatMessage
from src.models.citation import Citation
from src.models.conversation import Conversation
from src.models.document import Document, DocumentType
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember

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
    WorkspaceMember.__table__,
    Conversation.__table__,
    Thread.__table__,
    ChatMessage.__table__,
    Document.__table__,
    Citation.__table__,
]

_ZERO_WRITES = {"unlinked": 0, "copied_fields_cleared": 0}


async def _make_engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=_TABLES)
        )
        await connection.run_sync(_USERS.create)
    return engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = await _make_engine("sqlite+aiosqlite:///:memory:")
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

    async def workspace(self, owner: UUID, org: Optional[UUID]) -> UUID:
        ws = uuid4()
        await self.db.execute(
            insert(Workspace.__table__).values(
                id=ws, name="w", owner_id=owner, organization_id=org
            )
        )
        return ws

    async def member(self, ws: UUID, user: UUID, *, removed: bool = False) -> None:
        await self.db.execute(
            insert(WorkspaceMember.__table__).values(
                id=uuid4(),
                workspace_id=ws,
                user_id=user,
                role="viewer",
                is_deleted=removed,
            )
        )

    async def thread(self, ws: UUID, creator: UUID) -> UUID:
        conv, thread = uuid4(), uuid4()
        await self.db.execute(
            insert(Conversation.__table__).values(
                id=conv, workspace_id=ws, title="c", created_by_id=creator
            )
        )
        await self.db.execute(
            insert(Thread.__table__).values(
                id=thread, conversation_id=conv, created_by_id=creator
            )
        )
        return thread

    async def message(self, thread: UUID, author: Optional[UUID] = None) -> UUID:
        msg = uuid4()
        await self.db.execute(
            insert(ChatMessage.__table__).values(
                id=msg,
                thread_id=thread,
                user_id=author,
                role="user" if author else "assistant",
                content="m",
            )
        )
        return msg

    async def document(self, org: UUID, title: str, *, deleted: bool = False) -> UUID:
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
                is_deleted=deleted,
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
    org_a, org_b, org_c = uuid4(), uuid4(), uuid4()
    async with factory() as db:
        w = _World(db)
        owner_a = await w.user(org_a)
        member_b = await w.user(org_b)
        removed_c = await w.user(org_c)
        creator_c = await w.user(org_c)
        orphan = await w.user(None)

        ws = await w.workspace(owner_a, org_a)
        await w.member(ws, member_b)
        await w.member(
            ws, removed_c, removed=True
        )  # former member: org_c no longer counts
        thread = await w.thread(ws, owner_a)
        bot = await w.message(thread)
        by_b = await w.message(thread, member_b)

        legacy_ws = await w.workspace(owner_a, None)  # NULL org -> owner's org
        legacy_bot = await w.message(await w.thread(legacy_ws, owner_a))

        creator_ws = await w.workspace(owner_a, org_a)
        creator_bot = await w.message(await w.thread(creator_ws, creator_c))

        unknown_ws = await w.workspace(orphan, None)
        unknown_bot = await w.message(await w.thread(unknown_ws, orphan))

        own_doc = await w.document(org_a, "ours")
        b_doc = await w.document(org_b, "member doc")
        c_doc = await w.document(org_c, "SECRET foreign title")
        deleted_doc = await w.document(org_a, "gone", deleted=True)

        ids = {
            "own": await w.citation(bot, own_doc, chunk_index=0),
            "foreign": await w.citation(
                bot,
                c_doc,
                chunk_index=1,
                snippet="client snippet",
                document_title="SECRET foreign title",
                document_type="pdf",
            ),
            "foreign_own_title": await w.citation(
                bot, c_doc, chunk_index=2, document_title="client label"
            ),
            # (A) cross-org member's own document, cited by the assistant and
            # by that member: both legitimate, both kept.
            "member_doc_bot": await w.citation(bot, b_doc, chunk_index=3),
            "member_doc_author": await w.citation(by_b, b_doc, chunk_index=4),
            # (B) member of org B cites an org-A document; org A is the thread's
            # org but not the citing user's, so #2195 would have dropped it.
            "author_not_owner": await w.citation(
                by_b, own_doc, chunk_index=5, document_title="ours"
            ),
            "deleted": await w.citation(
                bot, deleted_doc, chunk_index=6, document_title="gone"
            ),
            "legacy_own": await w.citation(legacy_bot, own_doc, chunk_index=7),
            "legacy_foreign": await w.citation(legacy_bot, c_doc, chunk_index=8),
            "creator_org": await w.citation(creator_bot, c_doc, chunk_index=9),
            "unknown": await w.citation(unknown_bot, c_doc, chunk_index=10),
            "external": await w.citation(
                bot, None, chunk_index=11, external_reference_id="arxiv:1"
            ),
            "standalone": await w.citation(None, c_doc, chunk_index=12),
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


_FIRST_PASS = {
    "scanned": 11,
    "cross_org": 4,
    "deleted_document": 1,
    "undetermined_org": 1,
    "would_unlink": 5,
    "would_clear_fields": 2,
}
_UNLINKED = (
    "foreign",
    "foreign_own_title",
    "author_not_owner",
    "legacy_foreign",
    "deleted",
)
_KEPT = (
    "own",
    "member_doc_bot",
    "member_doc_author",
    "legacy_own",
    "creator_org",
    "unknown",
    "external",
    "standalone",
)


@pytest.mark.asyncio
async def test_dry_run_reports_would_counts_audits_and_writes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(factory)
    before = await _rows(factory)
    audit: List[Dict[str, Any]] = []

    result = await neutralize(factory, apply=False, audit=audit.append)

    assert result == {**_FIRST_PASS, **_ZERO_WRITES}
    assert await _rows(factory) == before
    assert {UUID(r["citation_id"]) for r in audit} == {ids[k] for k in _UNLINKED}
    assert all(r["applied"] is False for r in audit)


@pytest.mark.asyncio
async def test_apply_unlinks_only_what_2195_would_have_dropped(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(factory)
    before = await _rows(factory)

    first = await neutralize(factory, apply=True)
    assert first == {**_FIRST_PASS, "unlinked": 5, "copied_fields_cleared": 2}

    after = await _rows(factory)
    for key in _UNLINKED:
        assert after[ids[key]].document_id is None, key
    for key in _KEPT:
        assert after[ids[key]] == before[ids[key]], key
    # Values copied from a foreign document are cleared; client text stays.
    assert after[ids["foreign"]].document_title is None
    assert after[ids["foreign"]].document_type is None
    assert after[ids["foreign"]].snippet == "client snippet"
    assert after[ids["foreign_own_title"]].document_title == "client label"
    assert after[ids["author_not_owner"]].document_title is None
    # A soft-deleted document of the thread's own org: link dropped, title kept.
    assert after[ids["deleted"]].document_title == "gone"

    second = await neutralize(factory, apply=True)
    assert second == {
        "scanned": 6,
        "cross_org": 0,
        "deleted_document": 0,
        "undetermined_org": 1,
        "would_unlink": 0,
        "would_clear_fields": 0,
        **_ZERO_WRITES,
    }
    assert await _rows(factory) == after


@pytest.mark.asyncio
async def test_audit_records_allow_restore_and_carry_no_titles(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(factory)
    before = await _rows(factory)
    audit: List[Dict[str, Any]] = []

    await neutralize(factory, apply=True, audit=audit.append)

    by_id = {UUID(r["citation_id"]): r for r in audit}
    foreign = by_id[ids["foreign"]]
    assert foreign["applied"] is True
    assert foreign["reason"] == "cross_org"
    assert UUID(foreign["old_document_id"]) == before[ids["foreign"]].document_id
    assert foreign["fields_cleared"] == ["document_title", "document_type"]
    assert foreign["old_document_type"] == "pdf"
    assert {"doc_org", "thread_org", "author_org"} <= foreign.keys()
    assert by_id[ids["deleted"]]["reason"] == "deleted_document"
    assert by_id[ids["deleted"]]["fields_cleared"] == []
    dumped = json.dumps(audit)
    for title in ("SECRET", "client label", "ours", "gone"):
        assert title not in dumped


@pytest.mark.asyncio
async def test_concurrent_edit_is_not_overwritten(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the ``Citation.document_id == row.document_id`` guard in the UPDATE."""
    ids = await _seed(factory)
    async with factory() as db:
        replacement = (
            await db.execute(
                select(Citation.document_id).where(Citation.id == ids["own"])
            )
        ).scalar_one()

    original = script._member_orgs

    async def edit_between_read_and_write(db: AsyncSession, workspace_ids: Any) -> Any:
        # Someone relinks the foreign citation to a legitimate document after
        # the scan read it but before the script writes.
        await db.execute(
            update(Citation)
            .where(Citation.id == ids["foreign"])
            .values(document_id=replacement)
        )
        return await original(db, workspace_ids)

    monkeypatch.setattr(script, "_member_orgs", edit_between_read_and_write)
    result = await neutralize(factory, apply=True)

    assert result["unlinked"] == 4
    assert (await _rows(factory))[ids["foreign"]].document_id == replacement


@pytest.mark.asyncio
async def test_small_batches_give_the_same_result(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(factory)
    result = await neutralize(factory, apply=True, batch_size=1)
    assert result["unlinked"] == 5
    assert (await neutralize(factory, apply=True, batch_size=1))["would_unlink"] == 0


def _file_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'cit.db'}"

    async def seed() -> None:
        engine = await _make_engine(url)
        await _seed(async_sessionmaker(engine, expire_on_commit=False))
        await engine.dispose()

    asyncio.run(seed())

    def open_target() -> Any:
        engine = create_async_engine(url)
        return (
            async_sessionmaker(engine, expire_on_commit=False),
            "postgresql+asyncpg://svc:hunter2@db.internal:5432/app",
        )

    monkeypatch.setattr(script, "_open_target", open_target)
    return url


def test_main_dry_run_prints_would_counts_target_and_audit_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _file_db(tmp_path, monkeypatch)
    audit_file = tmp_path / "audit.ndjson"

    script.main(["--audit-file", str(audit_file)])

    out, err = capsys.readouterr()
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary["mode"] == "dry-run"
    assert summary["would_unlink"] == 5
    assert summary["would_clear_fields"] == 2
    assert "db.internal" in err and "hunter2" not in err
    lines = audit_file.read_text().splitlines()
    assert len(lines) == 5
    assert all(json.loads(line)["applied"] is False for line in lines)


def test_main_audits_to_stdout_without_audit_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _file_db(tmp_path, monkeypatch)

    script.main([])

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["event"] for r in records] == ["citation"] * 5 + ["summary"]


@pytest.mark.parametrize("abbrev", ["--a", "--ap", "--appl"])
def test_abbreviated_apply_is_rejected(
    abbrev: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_open() -> Any:
        raise AssertionError("an abbreviation reached the database")

    monkeypatch.setattr(script, "_open_target", must_not_open)
    with pytest.raises(SystemExit) as excinfo:
        script.main([abbrev])
    assert excinfo.value.code == 2
