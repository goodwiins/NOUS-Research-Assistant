"""GOO-766: the abandon against a real commit on PostgreSQL.

Before GOO-766 an abandon that landed while commit was flushing still
committed: 19 of 300 runs in merge-loop-cq's stress run. thread_offload now
checks again right before the DBAPI COMMIT, under the lock the abandon takes.
The documented bound: an abandoned thread's write lands only when its COMMIT
was already admitted when the abandon was decided.

``test_an_abandoned_write_lands_only_if_its_commit_was_in_flight`` builds each
case on purpose (GOO-798): the offloaded thread pauses at one stage of
``Session.commit``, the caller is cancelled and abandons it there, then the
thread is released. Every stage has one expected outcome, so the test does
not depend on how a race happens to fall on the runner. It used to count the
refusals among 300 randomly timed abandons and require at least 5; on a loaded
CI runner fewer abandons fell before the COMMIT and that count dropped below 5.

``test_randomly_timed_abandons_stay_within_the_bound`` keeps the random stress
for the bound itself: any landed abandoned write must have been admitted.

Mutation checks: make ``_gate_abandoned_commit`` (or ``_Handoff.admit_commit``)
never refuse and the ``flushing`` case lands its write. Make
``_refuse_abandoned_commit`` never refuse and the ``before_commit`` case still
does not land (the engine gate refuses it instead), but it fails on which
check refused it (GOO-799).

    ORCHESTRATION_TEST_DATABASE_URL=postgresql://... \\
        pytest -q backend/tests/unit/core/test_thread_offload_abandon_postgres.py
"""

import asyncio
import os
import random
import threading
import traceback
import uuid
from typing import Any, Callable, Dict, Iterator, List, Set

import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, registry

from src.core import thread_offload
from src.core.thread_offload import to_thread_finish_on_cancel

pytestmark = pytest.mark.unit

_URL_ENV = "ORCHESTRATION_TEST_DATABASE_URL"
_RUNS = 300
_WAIT = 30.0


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    url = os.getenv(_URL_ENV)
    if not url:
        pytest.skip(f"{_URL_ENV} is not set")
    engine = create_engine(url, pool_size=10, max_overflow=20)
    yield engine
    engine.dispose()


@pytest.fixture
def row_table(pg_engine: Engine) -> Iterator[Any]:
    """A fresh table and a class mapped to it; dropped afterwards."""
    name = f"offload_stress_{uuid.uuid4().hex[:12]}"
    table = Table(name, MetaData(), Column("v", Integer, primary_key=True))
    table.create(pg_engine)

    class Row:
        def __init__(self, v: int) -> None:
            self.v = v

    registry().map_imperatively(Row, table)
    try:
        yield Row
    finally:
        table.drop(pg_engine)


async def _all_sessions_closed(pg_engine: Engine) -> None:
    """Every abandoned thread closes its own Session when it is done."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAIT
    while pg_engine.pool.checkedout() and loop.time() < deadline:  # type: ignore[attr-defined]
        await asyncio.sleep(0.05)
    assert pg_engine.pool.checkedout() == 0  # type: ignore[attr-defined]


def _landed(pg_engine: Engine, row_table: Any) -> Set[int]:
    name = inspect(row_table).local_table.name
    with pg_engine.connect() as conn:
        return set(conn.execute(text(f'SELECT v FROM "{name}"')).scalars())


_SESSION_CHECK = "_refuse_abandoned_commit"
_ENGINE_GATE = "_gate_abandoned_commit"

# Stage the thread is paused at when the abandon is decided, and what must
# follow: (write lands, admitted_before_abandon, abandoned_mid_commit, the
# check that refused the commit). before_commit and flushing agree on the
# first three (nothing admitted, nothing in flight); only the refusing check
# tells them apart (GOO-799).
_STAGES = {
    # Before commit() starts: the Session-level before_commit check refuses.
    "before_commit": (False, False, False, _SESSION_CHECK),
    # commit() is flushing, before_commit already passed (the GOO-766 window):
    # only the engine commit gate stands between the abandon and the COMMIT.
    "flushing": (False, False, False, _ENGINE_GATE),
    # The gate admitted the COMMIT and it is on its way: it may land.
    "commit_in_flight": (True, True, True, None),
    # The COMMIT returned (after_commit), the thread has not finished yet.
    "commit_returned": (True, True, False, None),
}


def _refused_by(error: BaseException) -> str:
    """The function that raised ``error``: which of the two checks refused."""
    return traceback.extract_tb(error.__traceback__)[-1].name


@pytest.mark.parametrize("stage", sorted(_STAGES))
async def test_an_abandoned_write_lands_only_if_its_commit_was_in_flight(
    pg_engine: Engine, row_table: Any, stage: str
) -> None:
    reached = threading.Event()
    release = threading.Event()
    done = threading.Event()
    seen: Dict[str, Any] = {"refused_by": None}

    def _pause() -> None:
        seen["handoff"] = thread_offload._offloaded.handoff
        seen["committing"] = seen["handoff"]._committing
        reached.set()
        if not release.wait(_WAIT):
            raise RuntimeError("the test never released the paused thread")

    db = Session(bind=pg_engine)
    hooks: List[Any] = []

    def _hook(target: Any, name: str, fn: Callable[..., None]) -> None:
        event.listen(target, name, fn)
        hooks.append((target, name, fn))

    if stage == "flushing":
        _hook(db, "after_flush_postexec", lambda *_: _pause())
    elif stage == "commit_in_flight":
        # Engine listeners run after the class-level gate, so this pauses
        # with the COMMIT admitted and not yet sent. Only the offloaded thread
        # carries a handoff; the test's own connections pass straight through.
        def _in_commit(_conn: Any) -> None:
            if getattr(thread_offload._offloaded, "handoff", None) is not None:
                _pause()

        _hook(pg_engine, "commit", _in_commit)
    elif stage == "commit_returned":
        _hook(db, "after_commit", lambda *_: _pause())

    def _write(session: Session) -> None:
        try:
            session.add(row_table(1))
            if stage == "before_commit":
                _pause()
            try:
                session.commit()
            except thread_offload.AbandonedSessionError as error:
                seen["refused_by"] = _refused_by(error)
                raise
        finally:
            done.set()

    try:
        task = asyncio.create_task(
            to_thread_finish_on_cancel(_write, db, guard_session=db, max_wait=0)
        )
        assert await asyncio.to_thread(reached.wait, _WAIT), "never reached stage"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task  # max_wait=0: abandoned while the thread is paused
        handoff = seen["handoff"]
        assert handoff is not None and handoff._abandoned
        release.set()
        # before_commit has no connection checked out yet, so the pool being
        # empty does not show the thread is done: wait for it explicitly.
        assert await asyncio.to_thread(done.wait, _WAIT), "thread never finished"
        await _all_sessions_closed(pg_engine)
    finally:
        release.set()
        for target, name, fn in hooks:
            event.remove(target, name, fn)

    landed = 1 in _landed(pg_engine, row_table)
    lands, admitted, mid_commit, refused_by = _STAGES[stage]
    assert seen["committing"] is mid_commit, "paused at the wrong point"
    assert (
        landed,
        handoff.admitted_before_abandon,
        handoff.abandoned_mid_commit,
        seen["refused_by"],
    ) == (lands, admitted, mid_commit, refused_by), stage
    # The guarantee itself: a landed write was admitted before the abandon.
    assert not landed or handoff.admitted_before_abandon


async def test_randomly_timed_abandons_stay_within_the_bound(
    pg_engine: Engine, row_table: Any
) -> None:
    """300 commits, each cancelled after a random delay with ``max_wait=0``
    so the abandon falls anywhere in the add/flush/COMMIT path. How many
    abandons fall where depends on the runner, so only the bound is checked;
    the test above covers each stage."""
    handoffs: Dict[int, Any] = {}
    rng = random.Random(766)

    def _write(db: Session, i: int) -> None:
        handoffs[i] = thread_offload._offloaded.handoff
        db.add(row_table(i))
        db.commit()

    for i in range(_RUNS):
        db = Session(bind=pg_engine)
        task = asyncio.create_task(
            to_thread_finish_on_cancel(_write, db, i, guard_session=db, max_wait=0)
        )
        await asyncio.sleep(rng.uniform(0, 0.004))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            db.close()  # finished before the cancel: the caller owns it

    await _all_sessions_closed(pg_engine)
    landed = _landed(pg_engine, row_table)

    abandoned = {i for i, h in handoffs.items() if h is not None and h._abandoned}
    # GOO-783: the bound is "no COMMIT is admitted after the abandon", so a
    # landed write is allowed when its COMMIT was admitted before the abandon,
    # in flight *or already done* (the COMMIT can return a few microseconds
    # before the thread calls finish()). admitted_before_abandon is captured
    # at the abandon, under the same lock admit_commit takes.
    admitted = {i for i in abandoned if handoffs[i].admitted_before_abandon}
    outside_bound = sorted((abandoned & landed) - admitted)
    print(
        f"runs={_RUNS} abandoned={len(abandoned)} "
        f"refused={len(abandoned - landed)} "
        f"landed_after_abandon={len(abandoned & landed)} "
        f"admitted_before_abandon={len(admitted)}"
    )
    assert not outside_bound, (
        f"{len(outside_bound)} abandoned writes landed although no COMMIT was "
        f"admitted when the abandon was decided: {outside_bound[:20]}"
    )
