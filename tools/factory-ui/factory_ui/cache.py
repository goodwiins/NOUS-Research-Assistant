"""Per-command result cache with a minimum interval between runs.

The limits hold whatever the caller: any number of requests, tabs or
refresh clicks start at most one run per minimum interval, and never two
runs of one command at a time. A failure is cached like a result.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .config import Policy


class CommandCache:
    def __init__(
        self,
        policy: Policy,
        clock: Callable[[], float],
        wall: Callable[[], float],
    ) -> None:
        self._policy = policy
        self._clock = clock
        self._wall = wall
        self._cond = threading.Condition()
        self._running = False
        self._result: dict[str, Any] | None = None
        self._result_at = 0.0
        self._last_start: float | None = None
        self._next_wall = 0.0

    def get(
        self, produce: Callable[[], dict[str, Any]], refresh: bool = False
    ) -> dict[str, Any]:
        with self._cond:
            now = self._clock()
            fresh = (
                self._result is not None and now - self._result_at < self._policy.ttl
            )
            may_run = (
                self._last_start is None
                or now - self._last_start >= self._policy.min_interval
            )
            if self._running:
                if self._result is not None and self._policy.serve_stale:
                    return self._stamped()
                while self._running:
                    self._cond.wait()
                return self._stamped()
            if self._result is not None and ((fresh and not refresh) or not may_run):
                return self._stamped()
            self._running = True
            self._last_start = now
            self._next_wall = self._wall() + self._policy.min_interval
        result: dict[str, Any] = {}
        try:
            result = produce()
        finally:
            with self._cond:
                result["as_of"] = self._wall()
                self._result = result
                self._result_at = self._clock()
                self._running = False
                self._cond.notify_all()
        with self._cond:
            return self._stamped()

    def _stamped(self) -> dict[str, Any]:
        assert self._result is not None
        return {**self._result, "next_refresh_at": self._next_wall}
