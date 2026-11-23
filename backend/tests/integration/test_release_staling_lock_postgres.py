"""Real PostgreSQL proof for GOO-721: release staling reads the evidence
graph before it queues on the Collection lock.

A staling write (here: withdrawing a claim's evidence link through the
route) reads the graph edges while another writer still holds the project's
Collection ``FOR UPDATE`` lock, then only confirms under the lock that the
decision ledger has not moved. A writer that commits in between moves the
ledger, so the edges are read again under the lock and a release promoted
in that window is still stamped stale.

Mutation verification (docs/engineering/testing.md):

- ``invalidate_dependents`` ignoring the confirmed snapshot (calling
  ``_build_edges`` under the lock every time, in ``locked_edges``):
  ``test_staling_write_reads_edges_before_the_lock`` fails, the edges are
  read a second time after the lock is granted;
- ``resolve_for_staling_write`` skipping the unlocked snapshot: the same
  test fails, no edges are read while the other writer holds the lock;
- ``confirm_edges`` skipping the ledger comparison (always confirming):
  ``test_release_promoted_between_snapshot_and_lock_is_staled`` fails, the
  release promoted after the snapshot keeps ``stale_at`` NULL.

Command (from the repo root): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
backend/tests/integration/test_release_staling_lock_postgres.py``.
"""

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any, TypeAlias, cast
from uuid import UUID

import pytest
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.research import claims as claims_api
from src.models.collection import CollectionDocument
from src.models.generated_draft import GeneratedDraft
from src.services.research import draft_release_service as svc
from src.services.research_engine.project_access import ResearchAction, resolve_project
from src.shared.claim_schemas import ClaimLinkCreate
from tests.integration.test_draft_release_postgres import (
    _assess,
    _body,
    _claim,
    _document,
    _events,
    _link,
    _promote,
    _row,
    _span_link,
    _user,
)
from tests.integration.test_report_identity_postgres import _seed, _wait_until_blocked
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Factory: TypeAlias = async_sessionmaker[AsyncSession]
SOURCE = "Methods. We enrolled 412 participants. Discussion follows."
QUOTE = "We enrolled 412 participants."
SENTENCE = "The trial enrolled 412 participants."
CONTENT = f"## Results\n{SENTENCE}\n"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _world(factory: Factory) -> Any:
    """One draft sentence, claimed, linked to a source span and assessed."""
    ids = await _seed(factory)
    p1 = ids["collection"]
    async with factory() as db:
        d1 = await _document(db, ids["org"], "d1", SOURCE)
        db.add(CollectionDocument(collection_id=p1, document_id=d1))
        draft = GeneratedDraft(
            project_id=p1, version=1, title="v1", content=CONTENT, is_current=True
        )
        db.add(draft)
        await db.commit()
    w = SimpleNamespace(factory=factory, ids=ids, p1=p1, draft=cast(UUID, draft.id))
    w.claim = await _claim(w, w.draft, CONTENT, SENTENCE, "c1")
    w.link = await _link(w, w.claim, "l1", **_span_link(d1, SOURCE, QUOTE))
    await _assess(w, w.claim, "a1", [w.link])
    return w


async def _withdraw(db: AsyncSession, w: Any) -> None:
    """The real route: withdrawing the link stales releases built on it."""
    await claims_api.create_claim_link(
        w.p1,
        w.claim.id,
        ClaimLinkCreate(
            claim_version_id=w.claim.version.id,
            kind="source_span",
            status="withdrawn",
            supersedes_link_id=w.link,
            idempotency_key="withdraw-l1",
        ),
        Response(),
        current_user=_user(w.ids["O"]),
        db=db,
    )


def _count_edge_reads(monkeypatch: pytest.MonkeyPatch) -> list[UUID]:
    calls: list[UUID] = []
    real = svc._build_edges

    async def spy(db: AsyncSession, collection_id: UUID) -> list[svc.Edge]:
        calls.append(collection_id)
        return await real(db, collection_id)

    monkeypatch.setattr(svc, "_build_edges", spy)
    return calls


async def _pid(db: AsyncSession) -> int:
    return int((await db.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def test_staling_write_reads_edges_before_the_lock(
    screening_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = screening_factory
    w = await _world(factory)
    release, _ = await _promote(w, "A", w.draft, 1, "p1", _sha(CONTENT))
    reads = _count_edge_reads(monkeypatch)
    async with factory() as holder, factory() as writer, factory() as observer:
        # Another writer holds the Collection lock and changes nothing.
        await resolve_project(holder, w.p1, w.ids["O"], ResearchAction.EDIT)
        pid = await _pid(writer)
        attempt = asyncio.create_task(_withdraw(writer, w))
        try:
            await _wait_until_blocked(observer, pid)
            assert reads == [w.p1], "the edges must be read before the lock"
            await holder.commit()
            await attempt
        finally:
            if not attempt.done():
                attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
    assert reads == [w.p1], "an unmoved ledger must not re-read under the lock"
    assert (await _row(factory, release.id))["stale_at"] is not None
    (staled,) = await _events(factory, "release.staled")
    assert staled["release_ids"] == [str(release.id)]
    assert staled["changed_nodes"] == [f"link:{w.link}"]


async def test_release_promoted_between_snapshot_and_lock_is_staled(
    screening_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = screening_factory
    w = await _world(factory)
    reads = _count_edge_reads(monkeypatch)
    async with factory() as holder, factory() as writer, factory() as observer:
        context = await resolve_project(
            holder, w.p1, w.ids["A"], ResearchAction.RELEASE
        )
        pid = await _pid(writer)
        attempt = asyncio.create_task(_withdraw(writer, w))
        try:
            await _wait_until_blocked(observer, pid)
            assert reads == [w.p1]  # the snapshot holds no release yet
            release, replayed = await svc.promote(
                holder, context, w.ids["A"], w.draft, 1, _body(_sha(CONTENT), "p1")
            )
            assert not replayed  # committed: the ledger moved after the snapshot
            await attempt
        finally:
            if not attempt.done():
                attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
    assert (await _row(factory, release.id))[
        "stale_at"
    ] is not None, "a release promoted after the snapshot was missed"
    (staled,) = await _events(factory, "release.staled")
    assert staled["release_ids"] == [str(release.id)]
    assert reads == [w.p1, w.p1], "a moved ledger must re-read under the lock"
