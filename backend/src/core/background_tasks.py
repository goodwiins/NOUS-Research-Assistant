"""Fire-and-forget asyncio tasks that are neither garbage-collected nor silent.

The event loop keeps only a *weak* reference to a task, so a bare
``asyncio.create_task(coro)`` whose result is dropped can be collected before it
finishes, and an exception it raises is only reported (if ever) as "Task
exception was never retrieved" at garbage collection. ``spawn`` keeps a strong
reference until the task is done and logs a failure the moment it happens.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, Optional, Set

logger = logging.getLogger(__name__)

_tasks: Set["asyncio.Task[Any]"] = set()


def _on_done(task: "asyncio.Task[Any]") -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Background task %s failed",
            task.get_name(),
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def spawn(
    coro: Coroutine[Any, Any, Any],
    *,
    name: Optional[str] = None,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> "asyncio.Task[Any]":
    """Schedule ``coro`` on ``loop`` (default: the running loop) and keep it alive.

    Raises ``RuntimeError`` like ``asyncio.create_task`` when no loop is running
    and none is given.
    """
    target = loop if loop is not None else asyncio.get_running_loop()
    task = target.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_on_done)
    return task


def pending_count() -> int:
    """Number of spawned tasks not yet finished (for tests and diagnostics)."""
    return len(_tasks)
