"""Verify that memory_save_node dispatches slow persistence off the critical path.

memory_save_node must return in well under 5 s even when save_memory (embed+PG)
and extract_insights (LLM) are slow, and the background task must eventually
complete. Gate-skip turns must dispatch NO background task.

The in-flight set is capped (drops logged + counted), the shutdown drain waits
then cancels with a bounded timeout, and a cancelled write is logged + counted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.services.agent._nodes_memory import (
    _BACKGROUND_TASKS,
    drain_memory_persist_tasks,
    memory_save_node,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SLOW_DELAY = 5  # seconds — slow enough that inline execution would be obvious

_NODES_LOGGER = "src.services.agent._nodes_memory"
_DROPPED_HELPER = "src.services.agent.observability.record_memory_persist_dropped"


def _config(thread_id: str = "thread-bg") -> dict:
    # Ids-only configurable (audit B8)
    return {
        "configurable": {
            "user_id": "user-bg-test",
            "thread_id": thread_id,
        }
    }


def _tool_bearing_state() -> dict:
    """State that PASSES the save gate: research intent + tool executed."""
    return {
        "messages": [
            HumanMessage(content="find papers on diffusion models"),
            AIMessage(content="Here are 5 relevant papers…"),
        ],
        "intent": "research",
        "tool_executions": [{"tool_name": "search_arxiv", "status": "completed"}],
    }


def _general_no_tool_state() -> dict:
    """State that FAILS the save gate: general intent, no tools."""
    return {
        "messages": [
            HumanMessage(content="hi there"),
            AIMessage(content="Hello! How can I help?"),
        ],
        "intent": "general",
        "tool_executions": [],
    }


# ---------------------------------------------------------------------------
# Test 1 — node returns quickly; background task eventually runs
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_memory_save_returns_before_slow_save_completes() -> None:
    """memory_save_node must NOT block on the 5-second save_memory stub.

    The drain (asyncio.gather) is performed INSIDE the patch context so the
    mocks are still active when the background coroutine actually executes.
    """
    ran: list[bool] = []
    store = MagicMock()

    async def slow_save(*args, **kwargs) -> None:  # type: ignore[misc]
        await asyncio.sleep(_SLOW_DELAY)
        ran.append(True)

    with (
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=store),
        ),
        patch(
            "src.services.agent.memory.save_memory",
            new=slow_save,
        ),
        patch(
            "src.services.agent.iteration_ledger.write_iteration",
        ),
    ):
        start = time.monotonic()
        result = await memory_save_node(_tool_bearing_state(), _config())
        elapsed = time.monotonic() - start

        # Node must return well before the 5-second stub would complete.
        assert (
            elapsed < 1.0
        ), f"memory_save_node blocked for {elapsed:.2f}s (expected < 1s)"
        # State shape is unchanged — node still returns {}.
        assert result == {}
        # save_memory has NOT run yet (it's in a background task).
        assert not ran, "save_memory ran synchronously — backgrounding did not work"

        # Now drain the background task while patches are still active.
        if _BACKGROUND_TASKS:
            await asyncio.gather(*list(_BACKGROUND_TASKS), return_exceptions=True)

    # After drain, save_memory should have been called by the background task.
    assert ran, "save_memory was never called by the background task"


# ---------------------------------------------------------------------------
# Test 2 — save-gate skip dispatches NO background task
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_skip_dispatches_no_background_task() -> None:
    """General/no-tool turns are gated out before any task is dispatched."""
    store = MagicMock()
    save_mock = AsyncMock(return_value=None)
    task_count_before = len(_BACKGROUND_TASKS)

    with (
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=store),
        ),
        patch(
            "src.services.agent.memory.save_memory",
            new=save_mock,
        ),
        patch(
            "src.services.agent.iteration_ledger.write_iteration",
        ),
    ):
        result = await memory_save_node(_general_no_tool_state(), _config())

    # Gate must fire before dispatch — no new tasks.
    assert len(_BACKGROUND_TASKS) == task_count_before
    save_mock.assert_not_called()
    assert result == {}


# ---------------------------------------------------------------------------
# Test 3 — in-flight cap drops (logged + counted) instead of growing the set
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cap_full_drops_write_without_creating_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At the cap, one more turn creates no task and counts ``cap_full``."""
    release = asyncio.Event()
    store = MagicMock()

    async def blocking_save(*args, **kwargs) -> None:  # type: ignore[misc]
        await release.wait()

    dropped = MagicMock()
    caplog.set_level(logging.WARNING, logger=_NODES_LOGGER)
    with (
        patch("src.services.agent._nodes_memory._MAX_IN_FLIGHT_PERSISTS", 2),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=store),
        ),
        patch("src.services.agent.memory.save_memory", new=blocking_save),
        patch("src.services.agent.iteration_ledger.write_iteration"),
        patch(_DROPPED_HELPER, new=dropped),
    ):
        try:
            await memory_save_node(_tool_bearing_state(), _config("thread-cap-1"))
            await memory_save_node(_tool_bearing_state(), _config("thread-cap-2"))
            assert len(_BACKGROUND_TASKS) == 2
            in_flight = set(_BACKGROUND_TASKS)

            result = await memory_save_node(
                _tool_bearing_state(), _config("thread-cap-3")
            )

            assert result == {}
            assert len(_BACKGROUND_TASKS) == 2
            assert set(_BACKGROUND_TASKS) == in_flight
            dropped.assert_called_once_with("cap_full")
            drop_logs = [
                r.getMessage()
                for r in caplog.records
                if r.levelno == logging.WARNING and "at cap" in r.getMessage()
            ]
            assert len(drop_logs) == 1
            assert "thread-cap-3" in drop_logs[0]
            # Never the user's query text.
            assert "diffusion" not in drop_logs[0]
        finally:
            release.set()
            await asyncio.gather(*list(_BACKGROUND_TASKS), return_exceptions=True)


# ---------------------------------------------------------------------------
# Test 4 — shutdown drain lets a slow-but-finite write finish
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_drain_waits_for_in_flight_write() -> None:
    """A 0.1 s write completes inside a 2 s drain; the set ends empty."""
    ran: list[bool] = []
    store = MagicMock()

    async def short_save(*args, **kwargs) -> None:  # type: ignore[misc]
        await asyncio.sleep(0.1)
        ran.append(True)

    dropped = MagicMock()
    with (
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=store),
        ),
        patch("src.services.agent.memory.save_memory", new=short_save),
        patch("src.services.agent.iteration_ledger.write_iteration"),
        patch(_DROPPED_HELPER, new=dropped),
    ):
        await memory_save_node(_tool_bearing_state(), _config("thread-drain-ok"))
        assert not ran

        await drain_memory_persist_tasks(timeout=2)

    assert ran == [True]
    assert not _BACKGROUND_TASKS
    dropped.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5 — shutdown drain cancels writes still pending at the timeout
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_drain_timeout_cancels_and_logs_pending_write(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A write far slower than the drain budget is cancelled, logged, counted."""
    store = MagicMock()

    async def endless_save(*args, **kwargs) -> None:  # type: ignore[misc]
        await asyncio.sleep(60)

    dropped = MagicMock()
    caplog.set_level(logging.WARNING, logger=_NODES_LOGGER)
    with (
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=store),
        ),
        patch("src.services.agent.memory.save_memory", new=endless_save),
        patch("src.services.agent.iteration_ledger.write_iteration"),
        patch(_DROPPED_HELPER, new=dropped),
    ):
        await memory_save_node(_tool_bearing_state(), _config("thread-drain-slow"))
        (task,) = list(_BACKGROUND_TASKS)

        start = time.monotonic()
        await drain_memory_persist_tasks(timeout=0.1)
        elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"drain took {elapsed:.2f}s (expected < 2s)"
    assert task.cancelled()
    assert not _BACKGROUND_TASKS
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("still pending after 0.1s shutdown drain" in m for m in messages)
    assert any("persist cancelled" in m and "thread-drain-slow" in m for m in messages)
    dropped.assert_called_once_with("cancelled")


# ---------------------------------------------------------------------------
# Test 6 — a cancelled persist task is logged and counted, never silent
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_persist_is_logged_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancelling an in-flight write logs via ``_on_persist_done``."""
    store = MagicMock()

    async def endless_save(*args, **kwargs) -> None:  # type: ignore[misc]
        await asyncio.sleep(60)

    dropped = MagicMock()
    caplog.set_level(logging.WARNING, logger=_NODES_LOGGER)
    with (
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=store),
        ),
        patch("src.services.agent.memory.save_memory", new=endless_save),
        patch("src.services.agent.iteration_ledger.write_iteration"),
        patch(_DROPPED_HELPER, new=dropped),
    ):
        await memory_save_node(_tool_bearing_state(), _config("thread-cancel"))
        (task,) = list(_BACKGROUND_TASKS)
        assert "thread-cancel" in task.get_name()

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert not _BACKGROUND_TASKS
    cancel_logs = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "persist cancelled" in r.getMessage()
    ]
    assert len(cancel_logs) == 1
    assert task.get_name() in cancel_logs[0].getMessage()
    dropped.assert_called_once_with("cancelled")


# ---------------------------------------------------------------------------
# Test 7 — a write that resists cancellation cannot stretch the drain budget
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_drain_bounds_slow_cancellation_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A persist that delays its cancel unwind is abandoned after the grace,
    logged, and counted exactly once."""
    store = MagicMock()

    async def stubborn_save(*args, **kwargs) -> None:  # type: ignore[misc]
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # Slow cleanup far past the drain budget, then finish cancelled.
            await asyncio.sleep(3)
            raise

    dropped = MagicMock()
    caplog.set_level(logging.WARNING, logger=_NODES_LOGGER)
    with (
        patch("src.services.agent._nodes_memory._SHUTDOWN_CANCEL_GRACE_SECONDS", 0.1),
        patch(
            "src.services.agent.memory.get_memory_store",
            new=AsyncMock(return_value=store),
        ),
        patch("src.services.agent.memory.save_memory", new=stubborn_save),
        patch("src.services.agent.iteration_ledger.write_iteration"),
        patch(_DROPPED_HELPER, new=dropped),
    ):
        await memory_save_node(_tool_bearing_state(), _config("thread-stubborn"))
        (task,) = list(_BACKGROUND_TASKS)

        start = time.monotonic()
        await drain_memory_persist_tasks(timeout=0.1)
        elapsed = time.monotonic() - start

        assert elapsed < 2.0, f"drain took {elapsed:.2f}s (expected < 2s)"
        assert not task.done()
        messages = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "did not finish cancelling" in m and "thread-stubborn" in m
            for m in messages
        )
        dropped.assert_called_once_with("cancelled")

        # The abandoned task finishes cancelled later: no second count.
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert task.cancelled()
    assert not _BACKGROUND_TASKS
    dropped.assert_called_once_with("cancelled")
