"""Run blocking work in a thread without letting cancellation outrun it.

``asyncio.to_thread`` returns as soon as the awaiting task is cancelled, but
the thread keeps running. That is unsafe when the thread uses something the
caller's cleanup tears down: a request-scoped sync ``Session`` that the
``get_db_sync`` dependency closes as soon as the handler unwinds, or an
upload whose object must be deleted once it lands. ``to_thread_finish_on_cancel``
keeps waiting for the thread through any number of cancellations, then
re-raises ``CancelledError``.

The wait after a cancel is bounded (``max_wait``, default from
``THREAD_OFFLOAD_CANCEL_MAX_WAIT_SECONDS``, 30 s): a thread hung on a dead
socket must not keep a cancelled request, or shutdown, waiting forever. Past
the bound the thread is abandoned with a warning, as plain ``to_thread``
would have done at once.
"""

import asyncio
import logging
import os
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_DEFAULT_MAX_WAIT_SECONDS = 30.0
_MAX_WAIT_ENV = "THREAD_OFFLOAD_CANCEL_MAX_WAIT_SECONDS"


def _default_max_wait() -> float:
    raw = os.getenv(_MAX_WAIT_ENV)
    if not raw:
        return _DEFAULT_MAX_WAIT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r", _MAX_WAIT_ENV, raw)
        return _DEFAULT_MAX_WAIT_SECONDS
    return value if value > 0 else _DEFAULT_MAX_WAIT_SECONDS


async def _wait_through_cancellation(
    future: "asyncio.Future[Any]", max_wait: float
) -> "tuple[bool, bool]":
    """Wait until ``future`` is done, absorbing cancellations.

    Returns ``(cancelled, abandoned)``: whether we were cancelled meanwhile,
    and whether we stopped waiting because ``max_wait`` seconds passed after
    the first cancel with the future still running. The future itself is
    shielded, so a cancel never reaches the thread's wrapper.
    """
    loop = asyncio.get_running_loop()
    deadline: Optional[float] = None
    while not future.done():
        if deadline is None:
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                if not future.done():
                    deadline = loop.time() + max_wait
                    continue
                return True, False
            except BaseException:
                break  # the work itself failed; future.done() is now True
            continue
        remaining = deadline - loop.time()
        if remaining <= 0:
            return True, True
        try:
            await asyncio.wait({future}, timeout=remaining)
        except asyncio.CancelledError:
            pass  # a further cancel: keep waiting, same deadline
    return deadline is not None, False


async def to_thread_finish_on_cancel(
    fn: Callable[..., _T],
    *args: Any,
    on_cancel: Optional[Callable[[_T], Any]] = None,
    max_wait: Optional[float] = None,
    **kwargs: Any,
) -> _T:
    """``asyncio.to_thread(fn, *args, **kwargs)`` that outlives cancellation.

    If the caller is cancelled, wait up to ``max_wait`` seconds for ``fn`` to
    finish first. When it succeeded and ``on_cancel`` is given, run
    ``on_cancel(result)`` in a thread (same bound), then re-raise
    ``CancelledError``. A failure of ``fn`` or ``on_cancel`` after the cancel
    is logged, since nobody else will see it. Without a cancellation it
    behaves exactly like ``asyncio.to_thread``.
    """
    bound = max_wait if max_wait is not None else _default_max_wait()
    name = getattr(fn, "__qualname__", repr(fn))
    work = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    cancelled, abandoned = await _wait_through_cancellation(work, bound)
    if not cancelled:
        return work.result()  # re-raises fn's exception, if any

    if abandoned or work.cancelled():
        logger.warning(
            "abandoning %s: still running %.0fs after its caller was cancelled",
            name,
            bound,
        )
        raise asyncio.CancelledError()

    error = work.exception()
    if error is not None:
        logger.warning(
            "offloaded call failed after its caller was cancelled: %s",
            name,
            exc_info=error,
        )
    elif on_cancel is not None:
        cleanup = asyncio.ensure_future(asyncio.to_thread(on_cancel, work.result()))
        _, cleanup_abandoned = await _wait_through_cancellation(cleanup, bound)
        if cleanup_abandoned:
            logger.warning(
                "abandoning cleanup for %s: still running %.0fs after cancel",
                name,
                bound,
            )
        elif not cleanup.cancelled() and cleanup.exception() is not None:
            logger.warning(
                "cleanup after a cancelled offloaded call failed",
                exc_info=cleanup.exception(),
            )
    raise asyncio.CancelledError()
