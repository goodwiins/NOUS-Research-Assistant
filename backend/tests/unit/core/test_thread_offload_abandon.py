"""GOO-698: a thread abandoned by ``to_thread_finish_on_cancel`` is still safe.

After the bounded wait the caller re-raises ``CancelledError`` while the
thread runs on. Before this fix:

* ``get_db_sync`` closed the request Session under the thread, so the work
  done so far was rolled back while the rest went on in a fresh transaction
  and could commit;
* the upload's ``on_cancel`` delete never ran, and a late exception only
  showed up as "Task exception was never retrieved".

Mutation checks (each test fails with its guard removed, passes with it):

* ``test_an_abandoned_thread_cannot_commit_and_closes_its_session``: delete
  the ``raise AbandonedSessionError`` in ``_refuse_abandoned_commit``
  (row 2 is committed), or the ``guard_session.close()`` in
  ``_finish_abandoned`` (the pool connection stays checked out).
* ``test_get_db_sync_leaves_an_abandoned_session_to_its_thread``: drop the
  ``if not session_is_abandoned(db)`` check in ``core/database.get_db_sync``.
* ``test_abandoned_work_runs_its_cleanup_when_it_finishes``: drop the
  ``on_cancel(result)`` call in ``_finish_abandoned``.
* ``test_a_late_failure_is_logged_not_left_unretrieved``: re-raise instead of
  ``return None`` after ``_finish_abandoned`` in ``_start._run``.
* ``test_a_late_cleanup_failure_is_logged_not_left_unretrieved``: start the
  cleanup with ``asyncio.ensure_future(asyncio.to_thread(on_cancel, ...))``
  instead of ``_start(...)``.
* ``test_the_handoff_decides_once``: drop ``if self._finished: return False``
  in ``_Handoff.abandon``.

    pytest -q backend/tests/unit/core/test_thread_offload_abandon.py
"""

import asyncio
import contextlib
import gc
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from src.core import thread_offload
from src.core.thread_offload import session_is_abandoned, to_thread_finish_on_cancel

pytestmark = pytest.mark.unit


async def _eventually(pred: Callable[[], bool], timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


async def _abandon(task: "asyncio.Task[Any]", entered: threading.Event) -> None:
    """Cancel ``task`` once its thread is running and wait out ``max_wait``."""
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(
        f"sqlite:///{tmp_path / 'offload.db'}",
        connect_args={"check_same_thread": False},
    )
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE t (v INTEGER)"))
    yield eng
    eng.dispose()


@contextlib.contextmanager
def _unretrieved() -> Iterator[List[Dict[str, Any]]]:
    """Exceptions the running loop reports with nobody to receive them."""
    loop = asyncio.get_running_loop()
    seen: List[Dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: seen.append(context))
    try:
        yield seen
    finally:
        loop.set_exception_handler(previous)


async def _no_other_tasks() -> bool:
    me = asyncio.current_task()
    return await _eventually(
        lambda: all(t.done() for t in asyncio.all_tasks() if t is not me)
    )


async def test_an_abandoned_thread_cannot_commit_and_closes_its_session(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    db = sessionmaker(bind=engine)()
    entered, release = threading.Event(), threading.Event()

    def _write_slowly() -> None:
        db.execute(text("INSERT INTO t VALUES (1)"))
        entered.set()
        release.wait(timeout=5)
        db.execute(text("INSERT INTO t VALUES (2)"))
        db.commit()

    task = asyncio.create_task(
        to_thread_finish_on_cancel(_write_slowly, guard_session=db, max_wait=0.1)
    )
    try:
        await _abandon(task, entered)
        # The thread owns the Session now: get_db_sync must leave it alone.
        assert session_is_abandoned(db)
    finally:
        release.set()

    assert await _eventually(
        lambda: "abandoned offloaded call failed" in caplog.text
    ), "the late commit was not refused"
    assert "refusing to commit" in caplog.text
    assert await _eventually(
        lambda: engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    ), "the abandoned thread did not close its Session"

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT v FROM t")).fetchall()
    assert rows == [], f"an abandoned thread committed {rows}"


def test_get_db_sync_leaves_an_abandoned_session_to_its_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import database

    closed: List[object] = []

    class _Session:
        def __init__(self) -> None:
            self.info: Dict[str, Any] = {}

        def close(self) -> None:
            closed.append(self)

    monkeypatch.setattr(database, "SessionLocal", _Session)

    normal = database.get_db_sync()
    next(normal)
    normal.close()
    assert len(closed) == 1, "a normal request Session must still be closed"

    abandoned = database.get_db_sync()
    db = next(abandoned)
    assert thread_offload._Handoff(db).abandon()  # type: ignore[arg-type]
    abandoned.close()
    assert closed == [closed[0]], "closed a Session an abandoned thread still uses"


async def test_abandoned_work_runs_its_cleanup_when_it_finishes() -> None:
    entered, release = threading.Event(), threading.Event()
    cleaned: List[Any] = []

    def _upload() -> str:
        entered.set()
        release.wait(timeout=5)
        return "stored-key"

    task = asyncio.create_task(
        to_thread_finish_on_cancel(_upload, on_cancel=cleaned.append, max_wait=0.1)
    )
    try:
        await _abandon(task, entered)
        assert cleaned == []
    finally:
        release.set()
    assert await _eventually(
        lambda: cleaned == ["stored-key"]
    ), "the orphan delete never ran for an abandoned upload"


async def test_a_late_failure_is_logged_not_left_unretrieved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _unretrieved() as unretrieved:
        await _a_late_failure_is_logged_not_left_unretrieved(caplog, unretrieved)


async def _a_late_failure_is_logged_not_left_unretrieved(
    caplog: pytest.LogCaptureFixture, unretrieved: List[Dict[str, Any]]
) -> None:
    entered, release = threading.Event(), threading.Event()

    def _fails_late() -> None:
        entered.set()
        release.wait(timeout=5)
        raise RuntimeError("commit failed late")

    task = asyncio.create_task(to_thread_finish_on_cancel(_fails_late, max_wait=0.1))
    try:
        await _abandon(task, entered)
    finally:
        release.set()
    assert await _eventually(lambda: "commit failed late" in caplog.text)
    assert "abandoned offloaded call failed" in caplog.text
    assert await _no_other_tasks()
    del task
    gc.collect()
    assert unretrieved == [], [c.get("message") for c in unretrieved]


async def test_a_late_cleanup_failure_is_logged_not_left_unretrieved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _unretrieved() as unretrieved:
        await _a_late_cleanup_failure_is_logged_not_left_unretrieved(
            caplog, unretrieved
        )


async def _a_late_cleanup_failure_is_logged_not_left_unretrieved(
    caplog: pytest.LogCaptureFixture, unretrieved: List[Dict[str, Any]]
) -> None:
    entered, release = threading.Event(), threading.Event()
    cleanup_entered, cleanup_release = threading.Event(), threading.Event()

    def _bad_cleanup(_result: Any) -> None:
        cleanup_entered.set()
        cleanup_release.wait(timeout=5)
        raise OSError("storage down")

    def _upload() -> str:
        entered.set()
        release.wait(timeout=5)
        return "stored-key"

    task = asyncio.create_task(
        to_thread_finish_on_cancel(
            _upload,
            on_cancel=_bad_cleanup,
            max_wait=0.1,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        assert await asyncio.to_thread(cleanup_entered.wait, 5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert "abandoning cleanup" in caplog.text
    finally:
        cleanup_release.set()
    assert await _eventually(lambda: "storage down" in caplog.text)
    assert await _no_other_tasks()
    del task
    gc.collect()
    assert unretrieved == [], [c.get("message") for c in unretrieved]


def test_the_handoff_decides_once() -> None:
    finished_first = thread_offload._Handoff(None)
    assert finished_first.finish() is False
    assert finished_first.abandon() is False, "abandoned a thread that returned"

    abandoned_first = thread_offload._Handoff(None)
    assert abandoned_first.abandon() is True
    assert abandoned_first.finish() is True
