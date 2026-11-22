"""``spawn`` keeps fire-and-forget tasks alive and reports their failures (ST3)."""

from __future__ import annotations

import asyncio
import gc
import logging

import pytest

from src.core import background_tasks as bt

pytestmark = pytest.mark.unit


async def test_spawned_task_survives_gc_and_is_released_when_done() -> None:
    release = asyncio.Event()
    finished: list[bool] = []

    async def work() -> None:
        await release.wait()
        finished.append(True)

    before = bt.pending_count()
    bt.spawn(work(), name="gc-probe")  # reference deliberately dropped
    gc.collect()
    assert bt.pending_count() == before + 1

    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert finished == [True]
    assert bt.pending_count() == before


async def test_failure_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def boom() -> None:
        raise RuntimeError("kaput")

    with caplog.at_level(logging.ERROR, logger=bt.__name__):
        task = bt.spawn(boom(), name="boom-task")
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)

    assert any("boom-task" in r.getMessage() and r.exc_info for r in caplog.records)


def test_spawn_without_a_running_loop_raises() -> None:
    async def noop() -> None:
        return None

    coro = noop()
    with pytest.raises(RuntimeError):
        bt.spawn(coro)
    coro.close()


async def test_spawn_on_an_explicit_loop() -> None:
    async def value() -> int:
        return 7

    task = bt.spawn(value(), loop=asyncio.get_running_loop())
    assert await task == 7
