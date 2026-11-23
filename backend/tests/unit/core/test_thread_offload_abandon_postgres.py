"""GOO-766: stress the abandon against a real commit on PostgreSQL.

Before GOO-766 an abandon that landed while commit was flushing still
committed: 19 of 300 runs in merge-loop-cq's stress run. thread_offload now
checks again right before the DBAPI COMMIT, under the lock the abandon takes.
The documented bound: an abandoned thread's write lands only when its COMMIT
was already admitted when the abandon was decided.

This runs 300 offloaded commits, each cancelled after a random delay with
``max_wait=0`` so the abandon falls anywhere in the add/flush/COMMIT path, and
fails on any write that landed outside the bound.

Mutation check: make ``_gate_abandoned_commit`` never refuse and this test
reports writes that landed after the abandon.

    ORCHESTRATION_TEST_DATABASE_URL=postgresql://... \\
        pytest -q backend/tests/unit/core/test_thread_offload_abandon_postgres.py
"""

import asyncio
import os
import random
import uuid
from typing import Any, Dict, Iterator, Set

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, registry

from src.core import thread_offload
from src.core.thread_offload import to_thread_finish_on_cancel

pytestmark = pytest.mark.unit

_URL_ENV = "ORCHESTRATION_TEST_DATABASE_URL"
_RUNS = 300


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    url = os.getenv(_URL_ENV)
    if not url:
        pytest.skip(f"{_URL_ENV} is not set")
    engine = create_engine(url, pool_size=10, max_overflow=20)
    yield engine
    engine.dispose()


async def test_an_abandoned_write_lands_only_if_its_commit_was_in_flight(
    pg_engine: Engine,
) -> None:
    name = f"offload_stress_{uuid.uuid4().hex[:12]}"
    table = Table(name, MetaData(), Column("v", Integer, primary_key=True))
    table.create(pg_engine)

    class Row:
        def __init__(self, v: int) -> None:
            self.v = v

    registry().map_imperatively(Row, table)
    handoffs: Dict[int, Any] = {}
    rng = random.Random(766)

    def _write(db: Session, i: int) -> None:
        handoffs[i] = thread_offload._offloaded.handoff
        db.add(Row(i))
        db.commit()

    try:
        sessions = []
        for i in range(_RUNS):
            db = Session(bind=pg_engine)
            sessions.append(db)
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

        # Every abandoned thread closes its own Session when it is done.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30
        while pg_engine.pool.checkedout() and loop.time() < deadline:  # type: ignore[attr-defined]
            await asyncio.sleep(0.05)
        assert pg_engine.pool.checkedout() == 0  # type: ignore[attr-defined]

        with pg_engine.connect() as conn:
            landed: Set[int] = {
                v for (v,) in conn.execute(text(f'SELECT v FROM "{name}"'))
            }
    finally:
        table.drop(pg_engine)

    abandoned = {i for i, h in handoffs.items() if h is not None and h._abandoned}
    in_flight = {i for i in abandoned if handoffs[i].abandoned_mid_commit}
    outside_bound = sorted((abandoned & landed) - in_flight)
    refused = abandoned - landed
    print(
        f"runs={_RUNS} abandoned={len(abandoned)} refused={len(refused)} "
        f"landed_after_abandon={len(abandoned & landed)} in_flight={len(in_flight)}"
    )
    assert not outside_bound, (
        f"{len(outside_bound)} abandoned writes landed although no COMMIT was "
        f"in flight when the abandon was decided: {outside_bound[:20]}"
    )
    # The stress really reaches the abandon path and the gate refuses.
    assert len(refused) >= 5, "too few abandons were refused to exercise the gate"
