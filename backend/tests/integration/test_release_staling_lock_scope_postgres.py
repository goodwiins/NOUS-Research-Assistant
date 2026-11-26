"""Real PostgreSQL proof for GOO-721: release staling reads the graph before
the Collection lock and re-checks it under the lock.

Built on ``test_draft_release_postgres``'s world (GOO-307). A staling writer
resolves with ``draft_release_service.resolve_for_staling``: VIEW checks, the
edge snapshot with no lock held, then the Collection ``FOR UPDATE`` lock and a
stream-head comparison. Only a snapshot whose heads still match rides on the
context; otherwise ``invalidate_dependents`` rebuilds under the lock.

Mutation verification (docs/engineering/testing.md):

- head comparison dropped from ``resolve_for_staling`` (snapshot always
  trusted): ``test_release_promoted_before_the_lock_is_still_staled`` leaves
  the concurrently promoted release live;
- snapshot taken after ``resolve_project(action)`` (inside the lock): the
  same test's promotion blocks on the Collection lock and times out;
- ``edges`` ignored by ``invalidate_dependents`` (always rebuilt):
  ``test_current_snapshot_is_used_under_the_lock`` sees a second graph build;
- the no-live-release short circuit removed: ``test_no_live_release_builds
  _no_graph`` sees a graph build;
- stream heads compared by stream id only, ignoring ``next_seq``:
  ``test_existing_stream_advanced_before_the_lock_discards_the_snapshot``
  leaves the release promoted on an existing stream live;
- ``load_edges`` computing the ``changed`` set (source-pin hashing) before
  the lock: ``test_current_snapshot_is_used_under_the_lock`` sees it;
- a new link or first assessment resolving through ``resolve_for_staling``:
  ``test_writes_that_cannot_stale_skip_the_snapshot`` sees a snapshot;
- the identity-map expunge removed from ``snapshot_edges``:
  ``test_snapshot_leaves_no_rows_in_the_session`` finds the graph rows;
- ``_require_read_committed`` dropped from ``resolve_for_staling`` or from
  ``invalidate_dependents``, or accepting ``repeatable read`` (GOO-777):
  ``test_staling_refuses_a_repeatable_read_transaction`` sees no error.

Command (from ``backend/``): ``ORCHESTRATION_TEST_DATABASE_URL=... pytest -q
tests/integration/test_release_staling_lock_scope_postgres.py``.
"""

import asyncio
import json
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from sqlalchemy import event, select, text

from src.api.research import claims as claims_api
from src.models.draft_citation import DraftCitation
from src.models.draft_release import DraftRelease
from src.models.generated_draft import GeneratedDraft
from src.services.research import claims_service as claims
from src.services.research import draft_release_service as svc
from src.services.research_engine.project_access import resolve_project
from src.shared.claim_schemas import ClaimAssessmentCreate, ClaimLinkCreate
from tests.integration.test_draft_release_postgres import (
    ADJUDICATE,
    V2,
    V2_S,
    Factory,
    _assess,
    _claim,
    _events,
    _link,
    _promote,
    _setup,
    _sha,
)
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    VERSIONS,
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


async def _releasable(w: Any) -> tuple[UUID, Any, UUID]:
    """A current one-claim draft whose claim is linked and assessed:
    (draft, claim, assessment). Promotable by S or A; ``w.link`` is the link."""
    async with w.factory() as db:
        await db.execute(
            text("UPDATE generated_drafts SET is_current = false WHERE id = :d"),
            {"d": w.v1},
        )
        draft = GeneratedDraft(
            project_id=w.p1, version=2, title="v2", content=V2, is_current=True
        )
        db.add(draft)
        await db.flush()
        db.add(DraftCitation(draft_id=draft.id, citation_index=1, document_id=w.d2))
        await db.commit()
    draft_id = cast(UUID, draft.id)
    claim = await _claim(w, draft_id, V2, V2_S, "c")
    link = await _link(w, claim, "l", kind="extraction", accepted_value_id=w.design)
    assessment = await _assess(w, claim, "a", [link])
    w.link = link
    return draft_id, claim, assessment


async def _reassess(db: Any, w: Any, claim: Any, supersedes: UUID, key: str) -> Any:
    """The assess route: ``resolve_for_staling`` then ``claims.assess``."""
    context = await svc.resolve_for_staling(db, w.p1, w.ids["A"], ADJUDICATE)
    w.last_assessment, _ = await claims.assess(
        db,
        context,
        claim.id,
        w.ids["A"],
        ClaimAssessmentCreate(
            claim_version_id=claim.version.id,
            stance=cast(Any, "supporting"),
            link_ids=[w.link],
            rationale="re-adjudicated",
            idempotency_key=key,
            supersedes_assessment_id=supersedes,
        ),
    )
    return context


async def _stale_at(w: Any, release_id: UUID) -> Any:
    async with w.factory() as db:
        return (
            await db.execute(
                select(DraftRelease.stale_at).where(DraftRelease.id == release_id)
            )
        ).scalar_one()


def _count_builds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    builds: list[int] = []
    original = svc.load_edges

    async def counted(db: Any, collection_id: UUID) -> Any:
        builds.append(1)
        return await original(db, collection_id)

    monkeypatch.setattr(svc, "load_edges", counted)
    return builds


@pytest.mark.asyncio
async def test_release_promoted_before_the_lock_is_still_staled(
    screening_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot is read with no lock held, so a promotion commits while
    the writer is paused inside it; the heads then differ under the lock and
    the writer stales the new release from a rebuilt graph."""
    w = await _setup(screening_factory)
    draft_id, claim, assessment = await _releasable(w)
    snapshotted, resume = asyncio.Event(), asyncio.Event()
    original = svc.snapshot_edges

    async def paused(db: Any, collection_id: UUID) -> Any:
        snapshot = await original(db, collection_id)
        snapshotted.set()
        await resume.wait()
        return snapshot

    monkeypatch.setattr(svc, "snapshot_edges", paused)

    async def writer() -> Any:
        async with w.factory() as db:
            return await _reassess(db, w, claim, assessment, "a-re")

    task = asyncio.create_task(writer())
    try:
        await asyncio.wait_for(snapshotted.wait(), 10)
        # Fails with a timeout if the snapshot ran under the Collection lock.
        release, replayed = await asyncio.wait_for(
            _promote(w, "S", draft_id, 2, "s-race", _sha(V2)), 10
        )
        assert not replayed
        resume.set()
        context = await asyncio.wait_for(task, 10)
    finally:
        resume.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert context.release_edges is None, "a moved head must discard the snapshot"
    assert await _stale_at(w, release.id) is not None
    staled = await _events(screening_factory, "release.staled")
    assert [e["release_ids"] for e in staled] == [[str(release.id)]]


@pytest.mark.asyncio
async def test_current_snapshot_is_used_under_the_lock(
    screening_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No writer between snapshot and lock: the graph is built once, before
    the lock, and the release it reaches is staled."""
    w = await _setup(screening_factory)
    draft_id, claim, assessment = await _releasable(w)
    release, _ = await _promote(w, "S", draft_id, 2, "s-1", _sha(V2))
    builds = _count_builds(monkeypatch)
    hashed: list[int] = []
    original_changed = svc._changed_sources

    async def counted_changed(*args: Any, **kwargs: Any) -> Any:
        hashed.append(1)
        return await original_changed(*args, **kwargs)

    monkeypatch.setattr(svc, "_changed_sources", counted_changed)
    async with w.factory() as db:
        context = await _reassess(db, w, claim, assessment, "a-re")
    assert context.release_edges, "the pre-lock snapshot rides on the context"
    assert builds == [1], "one build, before the lock"
    assert hashed == [], "edges only; the changed set is the writer's"
    assert await _stale_at(w, release.id) is not None


@pytest.mark.asyncio
async def test_no_live_release_builds_no_graph(
    screening_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _setup(screening_factory)
    _draft, claim, assessment = await _releasable(w)
    builds = _count_builds(monkeypatch)
    async with w.factory() as db:
        context = await resolve_project(db, w.p1, w.ids["A"], ADJUDICATE)
        stamped = await svc.invalidate_dependents(
            db,
            collection_id=w.p1,
            changed={("assessment", str(assessment))},
            actor_id=w.ids["A"],
            actor_role="adjudicator",
            cause={"family": "test", "event_id": None, "kind": "test"},
        )
        await db.rollback()
    assert context.release_edges is None
    assert stamped == []
    assert builds == []
    async with w.factory() as db:
        await _reassess(db, w, claim, assessment, "a-re")
    assert builds == []


@pytest.mark.asyncio
async def test_snapshot_leaves_no_rows_in_the_session(
    screening_factory: Factory,
) -> None:
    """Rows read before the lock must not answer reads made after it. The
    identity map is weak, so the loaded rows are pinned here the way any
    lingering reference (a cycle awaiting collection) would keep them."""
    w = await _setup(screening_factory)
    draft_id, _claim_, _assessment = await _releasable(w)
    release, _ = await _promote(w, "S", draft_id, 2, "s-1", _sha(V2))
    async with w.factory() as db:
        pinned: list[Any] = []
        event.listen(
            db.sync_session,
            "loaded_as_persistent",
            lambda _session, instance: pinned.append(instance),
        )
        before = set(db.identity_map.keys())
        snapshot = await svc.snapshot_edges(db, w.p1)
        assert snapshot.edges and pinned
        assert set(db.identity_map.keys()) == before
        # A read after the lock sees the row as committed then, not as cached.
        await db.execute(
            text("UPDATE draft_releases SET rationale = 'later' WHERE id = :id"),
            {"id": release.id},
        )
        row = (
            await db.execute(select(DraftRelease).where(DraftRelease.id == release.id))
        ).scalar_one()
        assert row.rationale == "later"


@pytest.mark.asyncio
async def test_claims_export_skips_a_document_the_pins_read_missed(
    screening_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins and titles are separate reads; a document only the second one
    sees is left out of the package instead of raising KeyError."""
    w = await _setup(screening_factory)
    draft_id, _claim_, _assessment = await _releasable(w)
    original = claims.load_document_pins

    async def missing_d2(db: Any, document_ids: Any) -> Any:
        pins = await original(db, document_ids)
        pins.pop(w.d2, None)
        return pins

    monkeypatch.setattr(claims, "load_document_pins", missing_d2)
    async with w.factory() as db:
        context = await resolve_project(db, w.p1, w.ids["O"])
        content, _filename = await claims.export_package(db, context, draft_id)
    documents = json.loads(content)["body"]["documents"]
    assert str(w.d2) not in {d["id"] for d in documents}


@pytest.mark.asyncio
async def test_existing_stream_advanced_before_the_lock_discards_the_snapshot(
    screening_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release stream already exists, so a promotion between snapshot and
    lock adds no stream: only its ``next_seq`` moves. The writer must still
    discard the snapshot and stale the new release."""
    w = await _setup(screening_factory)
    draft_id, claim, assessment = await _releasable(w)
    first, _ = await _promote(w, "S", draft_id, 2, "s-1", _sha(V2))
    async with w.factory() as db:
        await _reassess(db, w, claim, assessment, "a-2")
    assert await _stale_at(w, first.id) is not None
    tip = cast(UUID, w.last_assessment.id)
    async with w.factory() as db:
        before = await svc._stream_heads(db, w.p1)

    snapshotted, resume = asyncio.Event(), asyncio.Event()
    original = svc.snapshot_edges

    async def paused(db: Any, collection_id: UUID) -> Any:
        snapshot = await original(db, collection_id)
        snapshotted.set()
        await resume.wait()
        return snapshot

    monkeypatch.setattr(svc, "snapshot_edges", paused)

    async def writer() -> Any:
        async with w.factory() as db:
            return await _reassess(db, w, claim, tip, "a-3")

    task = asyncio.create_task(writer())
    try:
        await asyncio.wait_for(snapshotted.wait(), 10)
        second, replayed = await asyncio.wait_for(
            _promote(w, "S", draft_id, 2, "s-2", _sha(V2)), 10
        )
        assert not replayed and second.id != first.id
        async with w.factory() as db:
            after = await svc._stream_heads(db, w.p1)
        assert [i for i, _ in after] == [i for i, _ in before], "no new stream"
        assert after != before, "an existing stream's next_seq moved"
        resume.set()
        context = await asyncio.wait_for(task, 10)
    finally:
        resume.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert context.release_edges is None, "a moved next_seq discards the snapshot"
    assert await _stale_at(w, second.id) is not None


def _user(user_id: UUID) -> Any:
    return cast(Any, type("U", (), {"id": user_id})())


@pytest.mark.asyncio
async def test_writes_that_cannot_stale_skip_the_snapshot(
    screening_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new link and a first assessment cannot stale a release, so their
    routes take the lock without reading the graph; a superseding one reads
    it before the lock."""
    w = await _setup(screening_factory)
    draft_id, claim, assessment = await _releasable(w)
    await _promote(w, "S", draft_id, 2, "s-1", _sha(V2))
    snapshots: list[int] = []
    original = svc.snapshot_edges

    async def counted(db: Any, collection_id: UUID) -> Any:
        snapshots.append(1)
        return await original(db, collection_id)

    monkeypatch.setattr(svc, "snapshot_edges", counted)
    async with w.factory() as db:
        await claims_api.create_claim_link(
            w.p1,
            claim.id,
            ClaimLinkCreate(
                claim_version_id=claim.version.id,
                kind=cast(Any, "extraction"),
                accepted_value_id=w.design,
                idempotency_key="l-new",
            ),
            Response(),
            current_user=_user(w.ids["O"]),
            db=db,
        )
    async with w.factory() as db:
        # Refused under the lock (the tip already has an assessment); what
        # matters is that the route resolved without a snapshot.
        with pytest.raises(HTTPException):
            await claims_api.create_claim_assessment(
                w.p1,
                claim.id,
                ClaimAssessmentCreate(
                    claim_version_id=claim.version.id,
                    stance=cast(Any, "supporting"),
                    link_ids=[w.link],
                    rationale="first",
                    idempotency_key="a-first",
                ),
                Response(),
                current_user=_user(w.ids["A"]),
                db=db,
            )
    assert snapshots == []
    async with w.factory() as db:
        await claims_api.create_claim_assessment(
            w.p1,
            claim.id,
            ClaimAssessmentCreate(
                claim_version_id=claim.version.id,
                stance=cast(Any, "supporting"),
                link_ids=[w.link],
                rationale="re-adjudicated",
                idempotency_key="a-re",
                supersedes_assessment_id=assessment,
            ),
            Response(),
            current_user=_user(w.ids["A"]),
            db=db,
        )
    assert snapshots == [1]


async def _repeatable_read(db: Any) -> None:
    """Start this session's transaction at REPEATABLE READ (as
    ``prisma_service._begin_snapshot`` does)."""
    await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    level = (await db.execute(text("SHOW transaction_isolation"))).scalar_one()
    assert level == "repeatable read"


@pytest.mark.asyncio
async def test_staling_refuses_a_repeatable_read_transaction(
    screening_factory: Factory,
) -> None:
    """GOO-777: under REPEATABLE READ the post-lock head re-read and the
    rebuild under the lock both see the transaction's first snapshot, so a
    release promoted in between would be missed silently. Both entry points
    refuse instead, and nothing is staled."""
    w = await _setup(screening_factory)
    draft_id, claim, assessment = await _releasable(w)
    release, _ = await _promote(w, "S", draft_id, 2, "s-1", _sha(V2))

    async with w.factory() as db:
        await _repeatable_read(db)
        with pytest.raises(RuntimeError, match=svc.STALING_ISOLATION_ERROR):
            await svc.resolve_for_staling(db, w.p1, w.ids["A"], ADJUDICATE)
        await db.rollback()

    async with w.factory() as db:
        await _repeatable_read(db)
        with pytest.raises(RuntimeError, match=svc.STALING_ISOLATION_ERROR):
            await svc.invalidate_dependents(
                db,
                collection_id=w.p1,
                changed={("assessment", str(assessment))},
                actor_id=w.ids["A"],
                actor_role="adjudicator",
                cause={"family": "test", "event_id": None, "kind": "test"},
            )
        await db.rollback()

    assert await _stale_at(w, release.id) is None
    # The default (READ COMMITTED) still stales it.
    async with w.factory() as db:
        level = (await db.execute(text("SHOW transaction_isolation"))).scalar_one()
        assert level == "read committed"
        await db.rollback()
        await _reassess(db, w, claim, assessment, "a-rc")
    assert await _stale_at(w, release.id) is not None
