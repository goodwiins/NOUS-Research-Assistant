"""GOO-671 backfill: unlink message citations whose document the thread may not show.

Before #2195, ``POST /api/v2/messages`` stored ``citations[].document_id`` from
the request unchecked, and the presenter falls back to the linked document's
title and type, so old rows can still show another organization's document
whenever the thread is reopened. #2195 keeps a citation's document only when
it belongs to the *citing user's* organization and is not soft-deleted.

The one-off script must match that as closely as stored data allows:

* User-authored messages: exact parity, so the document must be in the
  author's org (case B: the thread's own org is not enough), and an author
  with no org keeps nothing, like ``filter_owned_document_ids``.
* Assistant/system messages have no author, so the document may be in any org
  that legitimately takes part in the thread: workspace org, owner org, thread
  creator org, or an active member's org (case A: a cross-org member's own
  document is kept). A removed member's org does not count.
* Soft-deleted documents are unlinked, but their copied title stays (it is the
  thread's own org's data).
* Dry run by default, writes nothing, but reports would_* counts; every
  affected row is emitted as one NDJSON audit record in both modes, with no
  titles; the concurrent-edit guard holds; a rerun is a no-op.
* ``--apply`` needs ``--audit-file``; a batch's audit records are durable
  before it commits, and an audit write failure rolls the batch back.
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
from scripts.neutralize_cross_org_message_citations import AuditLog, neutralize
from src.models.base import Base
from src.models.chat_message import ChatMessage
from src.models.citation import Citation
from src.models.conversation import Conversation
from src.models.document import Document, DocumentType
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.services.threads.workspace_access import filter_owned_document_ids

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


class _Capture(AuditLog):
    """In-memory audit sink: keeps every record, sync is a no-op."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def write(self, record: Dict[str, Any]) -> None:
        self.records.append(record)

    def sync(self) -> None:
        pass

    def events(self, name: str) -> List[Dict[str, Any]]:
        return [r for r in self.records if r["event"] == name]


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
        no_org = await w.user(None)

        ws = await w.workspace(owner_a, org_a)
        await w.member(ws, member_b)
        await w.member(
            ws, removed_c, removed=True
        )  # former member: org_c no longer counts
        thread = await w.thread(ws, owner_a)
        bot = await w.message(thread)
        by_b = await w.message(thread, member_b)
        await w.member(ws, no_org)
        by_no_org = await w.message(thread, no_org)

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
            # An author with no org: #2195 keeps none of their documents,
            # even one of the thread's own org.
            "orgless_author": await w.citation(by_no_org, own_doc, chunk_index=13),
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
    "scanned": 12,
    "cross_org": 5,
    "deleted_document": 1,
    "undetermined_org": 1,
    "would_unlink": 6,
    "would_clear_fields": 2,
}
_UNLINKED = (
    "foreign",
    "foreign_own_title",
    "author_not_owner",
    "legacy_foreign",
    "deleted",
    "orgless_author",
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
    audit = _Capture()

    result = await neutralize(factory, apply=False, audit=audit)

    assert result == {**_FIRST_PASS, **_ZERO_WRITES}
    assert await _rows(factory) == before
    cited = audit.events("citation")
    assert {UUID(r["citation_id"]) for r in cited} == {ids[k] for k in _UNLINKED}
    assert all(r["applied"] is False and r["staged"] is False for r in cited)
    assert audit.events("batch_committed") == []


@pytest.mark.asyncio
async def test_apply_unlinks_only_what_2195_would_have_dropped(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(factory)
    before = await _rows(factory)

    first = await neutralize(factory, apply=True, audit=_Capture())
    assert first == {**_FIRST_PASS, "unlinked": 6, "copied_fields_cleared": 2}

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

    second = await neutralize(factory, apply=True, audit=_Capture())
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
async def test_user_authored_citations_match_filter_owned_document_ids(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Differential: for every user-authored citation, the script keeps the
    link iff #2195's real ``filter_owned_document_ids`` would have kept it
    (an author without an org keeps nothing)."""
    ids = await _seed(factory)
    async with factory() as db:
        authored = (
            await db.execute(
                select(Citation.id, Citation.document_id, ChatMessage.user_id)
                .join(ChatMessage, ChatMessage.id == Citation.message_id)
                .where(
                    ChatMessage.user_id.is_not(None),
                    Citation.document_id.is_not(None),
                )
            )
        ).all()
        expected = {
            row.id: bool(
                await filter_owned_document_ids(db, [row.document_id], row.user_id)
            )
            for row in authored
        }
    assert ids["orgless_author"] in expected and not expected[ids["orgless_author"]]
    assert set(expected.values()) == {True, False}

    await neutralize(factory, apply=True, audit=_Capture())

    after = await _rows(factory)
    assert {cid: after[cid].document_id is not None for cid in expected} == expected


@pytest.mark.asyncio
async def test_audit_records_allow_restore_and_carry_no_titles(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(factory)
    before = await _rows(factory)
    audit = _Capture()

    await neutralize(factory, apply=True, audit=audit, target="sqlite://x")

    header = audit.records[0]
    assert header["event"] == "run_start" and header["mode"] == "apply"
    run_id = header["run_id"]
    assert all(r["run_id"] == run_id and r["timestamp"] for r in audit.records)
    by_id = {UUID(r["citation_id"]): r for r in audit.events("citation")}
    committed = {
        UUID(cid)
        for marker in audit.events("batch_committed")
        for cid in marker["applied_citation_ids"]
    }
    assert committed == {ids[k] for k in _UNLINKED}

    foreign = by_id[ids["foreign"]]
    # Written before the commit, so never claims to be applied by itself.
    assert foreign["applied"] is False and foreign["staged"] is True
    assert foreign["reason"] == "cross_org"
    assert UUID(foreign["old_document_id"]) == before[ids["foreign"]].document_id
    assert UUID(foreign["message_id"])
    assert foreign["fields_cleared"] == ["document_title", "document_type"]
    assert foreign["document_title_cleared"] is True
    assert foreign["old_document_type"] == "pdf"
    assert {"doc_org", "thread_org", "author_org", "batch"} <= foreign.keys()
    assert by_id[ids["foreign_own_title"]]["document_title_cleared"] is False
    assert by_id[ids["deleted"]]["reason"] == "deleted_document"
    assert by_id[ids["deleted"]]["fields_cleared"] == []
    dumped = json.dumps(audit.records)
    for title in ("SECRET", "client label", "ours", "gone", "client snippet"):
        assert title not in dumped


class _FailingSync(_Capture):
    def sync(self) -> None:
        if self.events("citation"):
            raise OSError("disk full")


@pytest.mark.asyncio
async def test_audit_write_failure_rolls_back_and_unlinks_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(factory)
    before = await _rows(factory)

    with pytest.raises(script.AuditWriteError):
        await neutralize(factory, apply=True, audit=_FailingSync())

    assert await _rows(factory) == before


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
    audit = _Capture()
    result = await neutralize(factory, apply=True, audit=audit)

    assert result["unlinked"] == 5
    assert (await _rows(factory))[ids["foreign"]].document_id == replacement
    committed = {
        cid
        for m in audit.events("batch_committed")
        for cid in m["applied_citation_ids"]
    }
    assert str(ids["foreign"]) not in committed


@pytest.mark.asyncio
async def test_small_batches_give_the_same_result(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(factory)
    audit = _Capture()
    result = await neutralize(factory, apply=True, batch_size=1, audit=audit)
    assert result["unlinked"] == 6
    assert len(audit.events("batch_committed")) == 6
    again = await neutralize(factory, apply=True, batch_size=1, audit=_Capture())
    assert again["would_unlink"] == 0


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


def _file_rows(url: str) -> Dict[UUID, Any]:
    async def read() -> Dict[UUID, Any]:
        engine = create_async_engine(url)
        try:
            return await _rows(async_sessionmaker(engine))
        finally:
            await engine.dispose()

    return asyncio.run(read())


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
    assert summary["would_unlink"] == 6
    assert summary["would_clear_fields"] == 2
    assert "db.internal" in err and "hunter2" not in err
    records = [json.loads(line) for line in audit_file.read_text().splitlines()]
    assert [r["event"] for r in records] == ["run_start"] + ["citation"] * 6 + [
        "summary"
    ]
    assert records[0]["target"] == "postgresql+asyncpg://db.internal:5432"
    assert summary["run_id"] == records[0]["run_id"]


def test_main_apply_appends_a_second_run_with_its_own_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _file_db(tmp_path, monkeypatch)
    audit_file = tmp_path / "audit.ndjson"

    script.main(["--audit-file", str(audit_file)])
    script.main(["--apply", "--audit-file", str(audit_file)])

    records = [json.loads(line) for line in audit_file.read_text().splitlines()]
    headers = [r for r in records if r["event"] == "run_start"]
    assert [h["mode"] for h in headers] == ["dry-run", "apply"]
    assert headers[0]["run_id"] != headers[1]["run_id"]
    assert [r["event"] for r in records].count("batch_committed") == 1


def test_main_audit_fsync_failure_exits_nonzero_and_unlinks_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = _file_db(tmp_path, monkeypatch)
    before = _file_rows(url)
    calls = {"n": 0}

    def failing_fsync(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] > 1:  # the run_start header syncs fine; the batch does not
            raise OSError("disk full")

    monkeypatch.setattr(script.os, "fsync", failing_fsync)
    with pytest.raises(SystemExit) as excinfo:
        script.main(["--apply", "--audit-file", str(tmp_path / "audit.ndjson")])

    assert excinfo.value.code == 1
    assert _file_rows(url) == before


def test_apply_requires_an_audit_file(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_open() -> Any:
        raise AssertionError("--apply without --audit-file reached the database")

    monkeypatch.setattr(script, "_open_target", must_not_open)
    with pytest.raises(SystemExit) as excinfo:
        script.main(["--apply"])
    assert excinfo.value.code == 2


def test_dry_run_audits_to_stdout_without_audit_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _file_db(tmp_path, monkeypatch)

    script.main([])

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["event"] for r in records] == ["run_start"] + ["citation"] * 6 + [
        "summary"
    ]


@pytest.mark.parametrize("abbrev", ["--a", "--ap", "--appl"])
def test_abbreviated_apply_is_rejected(
    abbrev: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_open() -> Any:
        raise AssertionError("an abbreviation reached the database")

    monkeypatch.setattr(script, "_open_target", must_not_open)
    # --audit-file is given, so only the abbreviation itself can be rejected.
    with pytest.raises(SystemExit) as excinfo:
        script.main([abbrev, "--audit-file", str(tmp_path / "a.ndjson")])
    assert excinfo.value.code == 2
