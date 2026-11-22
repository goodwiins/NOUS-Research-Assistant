"""Run blocking work in a thread without letting cancellation outrun it.

``asyncio.to_thread`` returns as soon as the awaiting task is cancelled, but
the thread keeps running. That is unsafe when the thread uses something the
caller's cleanup tears down: a request-scoped sync ``Session`` that the
``get_db_sync`` dependency closes as soon as the handler unwinds, or an
upload whose object must be deleted once it lands. ``to_thread_finish_on_cancel``
keeps waiting for the thread through any number of cancellations, then
re-raises ``CancelledError``.
"""

import asyncio
import logging
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


async def _wait_through_cancellation(future: "asyncio.Future[Any]") -> bool:
    """Wait until ``future`` is done; return True if we were cancelled meanwhile.

    The future itself is shielded, so a cancel never reaches the thread's
    wrapper; a second (or third) cancel is absorbed the same way.
    """
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break  # the work itself failed; future.done() is now True
    return cancelled


async def to_thread_finish_on_cancel(
    fn: Callable[..., _T],
    *args: Any,
    on_cancel: Optional[Callable[[_T], Any]] = None,
    **kwargs: Any,
) -> _T:
    """``asyncio.to_thread(fn, *args, **kwargs)`` that outlives cancellation.

    If the caller is cancelled, wait for ``fn`` to finish first, then, when
    ``fn`` succeeded and ``on_cancel`` is given, run ``on_cancel(result)`` in
    a thread (also to completion), and finally re-raise ``CancelledError``.
    Without a cancellation it behaves exactly like ``asyncio.to_thread``.
    """
    work = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    cancelled = await _wait_through_cancellation(work)
    if not cancelled:
        return work.result()  # re-raises fn's exception, if any

    if on_cancel is not None and work.exception() is None:
        cleanup = asyncio.ensure_future(asyncio.to_thread(on_cancel, work.result()))
        await _wait_through_cancellation(cleanup)
        if cleanup.exception() is not None:
            logger.warning(
                "cleanup after a cancelled offloaded call failed",
                exc_info=cleanup.exception(),
            )
    raise asyncio.CancelledError()
