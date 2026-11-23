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
the bound the thread is abandoned with a warning, and the thread itself takes
over what the caller would have done once it finishes (GOO-698):

* it runs ``on_cancel(result)`` (the upload's orphan delete), or logs the
  work's exception, instead of leaving it as "Task exception was never
  retrieved";
* with ``guard_session``, the Session is marked abandoned: a later commit on
  it raises ``AbandonedSessionError``, ``get_db_sync`` leaves it open instead
  of closing it under the thread, and the thread rolls it back and closes it
  when it is done. Closing it under the thread used to drop the work done so
  far while the rest of it went on in a fresh transaction and could commit.

Whether the thread is abandoned or still returns its result to the caller is
decided under one lock (``_Handoff``), so exactly one side does the cleanup.
"""

import asyncio
import logging
import os
import threading
from typing import Any, Callable, Optional, Tuple, TypeVar

from sqlalchemy import event
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_DEFAULT_MAX_WAIT_SECONDS = 30.0
_MAX_WAIT_ENV = "THREAD_OFFLOAD_CANCEL_MAX_WAIT_SECONDS"
_ABANDONED_KEY = "thread_offload.abandoned"


class AbandonedSessionError(InvalidRequestError):
    """A commit on a Session whose request gave up waiting for its thread."""


def session_is_abandoned(session: Session) -> bool:
    """True once a thread using ``session`` was abandoned after a cancel.

    The abandoned thread owns the Session from then on and closes it itself.
    """
    return bool(session.info.get(_ABANDONED_KEY))


@event.listens_for(Session, "before_commit")
def _refuse_abandoned_commit(session: Session) -> None:
    if session_is_abandoned(session):
        raise AbandonedSessionError(
            "refusing to commit: the request that owned this Session was "
            "cancelled and stopped waiting for this thread"
        )


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


class _Handoff:
    """Decides, once, whether the thread or its caller handles the outcome."""

    def __init__(self, guard_session: Optional[Session]) -> None:
        self._lock = threading.Lock()
        self._finished = False
        self._abandoned = False
        self._session = guard_session

    def abandon(self) -> bool:
        """Caller side: give the outcome to the thread, unless it is done."""
        with self._lock:
            if self._finished:
                return False  # its result is already on the way to the loop
            self._abandoned = True
            if self._session is not None:
                self._session.info[_ABANDONED_KEY] = True
            return True

    def finish(self) -> bool:
        """Thread side: True if the caller abandoned us and we must clean up."""
        with self._lock:
            self._finished = True
            return self._abandoned


def _finish_abandoned(
    name: str,
    result: Any,
    error: Optional[BaseException],
    on_cancel: Optional[Callable[[Any], Any]],
    guard_session: Optional[Session],
) -> None:
    """In the abandoned thread: do what the caller would have done."""
    if error is not None:
        logger.warning(
            "abandoned offloaded call failed when it finally finished: %s",
            name,
            exc_info=error,
        )
    elif on_cancel is not None:
        try:
            on_cancel(result)
        except Exception:
            logger.warning(
                "cleanup after an abandoned offloaded call failed: %s",
                name,
                exc_info=True,
            )
    if guard_session is not None:
        try:
            guard_session.close()  # rolls back whatever was not committed
        except Exception:
            logger.warning(
                "closing the Session of an abandoned offloaded call failed: %s",
                name,
                exc_info=True,
            )


def _start(
    fn: Callable[..., Any],
    args: Tuple[Any, ...],
    kwargs: Any,
    *,
    name: str,
    on_cancel: Optional[Callable[[Any], Any]] = None,
    guard_session: Optional[Session] = None,
) -> "Tuple[asyncio.Future[Any], _Handoff]":
    handoff = _Handoff(guard_session)

    def _run() -> Any:
        try:
            result = fn(*args, **kwargs)
        except BaseException as error:
            if handoff.finish():
                _finish_abandoned(name, None, error, on_cancel, guard_session)
                return None  # nobody awaits it: never leave it unretrieved
            raise
        if handoff.finish():
            _finish_abandoned(name, result, None, on_cancel, guard_session)
            return None
        return result

    return asyncio.ensure_future(asyncio.to_thread(_run)), handoff


async def _wait_through_cancellation(
    future: "asyncio.Future[Any]",
    max_wait: float,
    handoff: _Handoff,
    *,
    already_cancelled: bool = False,
) -> "tuple[bool, bool]":
    """Wait until ``future`` is done, absorbing cancellations.

    Returns ``(cancelled, abandoned)``: whether we were cancelled meanwhile,
    and whether we stopped waiting because ``max_wait`` seconds passed after
    the first cancel with the thread still running (it then owns its
    outcome, see ``_Handoff``). With ``already_cancelled`` the bound runs
    from the start. The future itself is shielded, so a cancel never
    reaches the thread's wrapper.
    """
    loop = asyncio.get_running_loop()
    deadline: Optional[float] = loop.time() + max_wait if already_cancelled else None
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
            if handoff.abandon():
                return True, True
            # The thread returned just now; its result reaches the loop soon.
            deadline = loop.time() + max_wait
            continue
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
    guard_session: Optional[Session] = None,
    **kwargs: Any,
) -> _T:
    """``asyncio.to_thread(fn, *args, **kwargs)`` that outlives cancellation.

    If the caller is cancelled, wait up to ``max_wait`` seconds for ``fn`` to
    finish first. When it succeeded and ``on_cancel`` is given, run
    ``on_cancel(result)`` in a thread (same bound), then re-raise
    ``CancelledError``. A failure of ``fn`` or ``on_cancel`` after the cancel
    is logged, since nobody else will see it. If the bound runs out, the
    thread is abandoned and does all of that itself when it finishes.

    Pass the request's Session as ``guard_session`` when ``fn`` uses it, so an
    abandoned thread can never commit on it and is the one to close it.

    Without a cancellation it behaves exactly like ``asyncio.to_thread``.
    """
    bound = max_wait if max_wait is not None else _default_max_wait()
    name = getattr(fn, "__qualname__", repr(fn))
    work, handoff = _start(
        fn,
        args,
        kwargs,
        name=name,
        on_cancel=on_cancel,
        guard_session=guard_session,
    )
    cancelled, abandoned = await _wait_through_cancellation(work, bound, handoff)
    if work.cancelled():
        # The task itself was cancelled (loop shutdown): the thread runs on
        # with nobody to receive its outcome, so it handles it itself.
        abandoned = handoff.abandon() or abandoned
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
        cleanup, cleanup_handoff = _start(
            on_cancel, (work.result(),), {}, name=f"cleanup for {name}"
        )
        _, cleanup_abandoned = await _wait_through_cancellation(
            cleanup, bound, cleanup_handoff, already_cancelled=True
        )
        if cleanup.cancelled():
            cleanup_handoff.abandon()
        if cleanup_abandoned:
            # Its thread logs a late failure itself (_finish_abandoned).
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
