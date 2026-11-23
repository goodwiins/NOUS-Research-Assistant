"""GOO-693: blocking work offloaded to a thread must not be outrun by cancellation.

A cancelled ``asyncio.to_thread`` returns at once while the thread keeps
going, so the caller's cleanup (closing the request's sync Session, deleting
an upload that is still landing) could run mid-use. A second cancel used to
skip the upload's orphan delete altogether.
"""

import asyncio
import threading
from typing import Any, List

import pytest

from src.core.thread_offload import to_thread_finish_on_cancel

pytestmark = pytest.mark.unit


def _blocking(
    entered: threading.Event, release: threading.Event, log: List[str]
) -> str:
    entered.set()
    release.wait(timeout=5)
    log.append("work done")
    return "stored-key"


async def test_without_cancellation_it_is_to_thread() -> None:
    loop_thread = threading.current_thread()
    seen: List[threading.Thread] = []

    def _work(x: int) -> int:
        seen.append(threading.current_thread())
        return x * 2

    assert await to_thread_finish_on_cancel(_work, 21) == 42
    assert seen and seen[0] is not loop_thread


async def test_errors_propagate() -> None:
    def _boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await to_thread_finish_on_cancel(_boom)


@pytest.mark.parametrize("cancels", [1, 3])
async def test_cancellation_waits_for_the_thread_then_cleans_up(cancels: int) -> None:
    entered, release = threading.Event(), threading.Event()
    log: List[str] = []

    def _cleanup(result: Any) -> None:
        log.append(f"cleanup {result}")

    task = asyncio.create_task(
        to_thread_finish_on_cancel(_blocking, entered, release, log, on_cancel=_cleanup)
    )
    assert await asyncio.to_thread(entered.wait, 5)
    for _ in range(cancels):  # a client disconnect, then shutdown, ...
        task.cancel()
        await asyncio.sleep(0.02)
    # Still waiting: the caller's teardown cannot run while the thread works.
    assert not task.done(), "returned while the thread was still running"

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert log == ["work done", "cleanup stored-key"]


async def test_failed_work_after_cancellation_skips_cleanup() -> None:
    entered, release = threading.Event(), threading.Event()
    cleaned: List[Any] = []

    def _fails() -> None:
        entered.set()
        release.wait(timeout=5)
        raise RuntimeError("upload failed: nothing stored")

    task = asyncio.create_task(
        to_thread_finish_on_cancel(_fails, on_cancel=cleaned.append)
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned == []


async def test_cleanup_failure_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entered, release = threading.Event(), threading.Event()

    def _bad_cleanup(_result: Any) -> None:
        raise OSError("storage down")

    task = asyncio.create_task(
        to_thread_finish_on_cancel(
            _blocking, entered, release, [], on_cancel=_bad_cleanup
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "cleanup after a cancelled offloaded call failed" in caplog.text


async def test_a_hung_thread_is_abandoned_after_the_max_wait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entered, release = threading.Event(), threading.Event()
    cleaned: List[Any] = []

    task = asyncio.create_task(
        to_thread_finish_on_cancel(
            _blocking, entered, release, [], on_cancel=cleaned.append, max_wait=0.1
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        release.set()
    assert "abandoning" in caplog.text
    # Its result is unknown when we give up, so no cleanup runs for it.
    assert cleaned == []


async def test_max_wait_defaults_from_env(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("THREAD_OFFLOAD_CANCEL_MAX_WAIT_SECONDS", "0.1")
    entered, release = threading.Event(), threading.Event()
    task = asyncio.create_task(
        to_thread_finish_on_cancel(_blocking, entered, release, [])
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        release.set()
    assert "abandoning" in caplog.text


async def test_work_failing_after_cancellation_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entered, release = threading.Event(), threading.Event()

    def _fails() -> None:
        entered.set()
        release.wait(timeout=5)
        raise RuntimeError("commit failed")

    task = asyncio.create_task(to_thread_finish_on_cancel(_fails))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "offloaded call failed after its caller was cancelled" in caplog.text
    assert "commit failed" in caplog.text
