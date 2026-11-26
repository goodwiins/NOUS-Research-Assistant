"""The only module that starts a process: two fixed helper commands.

Both are read-only in the helper's code (see README.md, "Allowlist"). The
command name comes from the server's route table, never from request text,
and run() refuses any other name before it spawns anything.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Any

from . import config
from .cache import CommandCache
from .readers import loads

_F = str(config.FACTORY_ROOT / "bin" / "factory")
ALLOWED: dict[str, tuple[str, str]] = {
    "doctor": (_F, "doctor"),
    "budget": (_F, "budget"),
}

CHILD_ENV_NAMES = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
TERM_GRACE = 5


def argv_for(name: str, root: Path) -> tuple[str, str]:
    """The fixed argv of an allowed command, with the helper under `root`."""
    if name not in ALLOWED:
        raise ValueError(f"command not allowed: {name!r}")
    return (str(root / "bin" / "factory"), ALLOWED[name][1])


def child_env(name: str, session_id: str | None) -> dict[str, str]:
    """The child's environment, built from scratch. Only `budget` gets a
    session id, and only one that matches SESSION_RE."""
    if name not in ALLOWED:
        raise ValueError(f"command not allowed: {name!r}")
    env = {k: os.environ[k] for k in CHILD_ENV_NAMES if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if session_id is not None:
        if name != "budget" or not SESSION_RE.fullmatch(session_id):
            raise ValueError("session id refused")
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    return env


def _status(code: int) -> str:
    if code in (0, 1):
        return "answer"
    if code == 6:
        return "helper bug"
    return "error"


def run(
    name: str,
    root: Path = config.FACTORY_ROOT,
    session_id: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run one allowed command and describe its outcome. Never raises for
    the child's behaviour: a timeout, a crash or bad output is a result."""
    argv = argv_for(name, root)
    env = child_env(name, session_id)
    if timeout is None:
        timeout = config.COMMANDS[name].timeout
    out: dict[str, Any] = {
        "status": "error",
        "exit": None,
        "data": None,
        "stderr": "",
        "error": None,
    }
    try:
        # stdout and stderr as pipes (capture_output=True), drained by
        # threads with bounded buffers, so a timeout or an oversize output
        # can stop the whole process group the child leads.
        proc = subprocess.Popen(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except OSError as e:
        out["error"] = f"could not start the helper: {e.strerror or type(e).__name__}"
        return out
    assert proc.stdout is not None and proc.stderr is not None
    stdout = _Drain(
        proc.stdout, config.STDOUT_CAP, lambda: _signal(proc, signal.SIGTERM)
    )
    stderr = _Drain(proc.stderr, config.STDERR_CAP * 4, None)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_group(proc)
        _finish(proc, stdout, stderr)
        out["status"] = "timeout"
        out["error"] = f"no answer within {timeout:g} s"
        return out
    _finish(proc, stdout, stderr)
    out["exit"] = proc.returncode
    out["stderr"] = stderr.text()[: config.STDERR_CAP]
    if stdout.over:
        out["error"] = "output larger than 1 MB"
        return out
    try:
        data = loads(stdout.text())
    except ValueError:
        out["error"] = "output is not JSON"
        return out
    if not isinstance(data, dict):
        out["error"] = "output is not a JSON object"
        return out
    out["data"] = data
    out["status"] = _status(proc.returncode)
    return out


class _Drain(threading.Thread):
    """Reads a pipe to its end and keeps at most `cap` bytes. When more
    arrive it sets `over` and calls `on_over` once; the rest is discarded."""

    def __init__(
        self, pipe: IO[bytes], cap: int, on_over: Callable[[], None] | None
    ) -> None:
        super().__init__(daemon=True)
        self._fd = pipe.fileno()
        self._cap = cap
        self._on_over = on_over
        self._chunks: list[bytes] = []
        self._size = 0
        self.over = False
        self.start()

    def run(self) -> None:
        while True:
            try:
                data = os.read(self._fd, 65536)
            except OSError:
                return
            if not data:
                return
            keep = data[: max(0, self._cap - self._size)]
            if keep:
                self._chunks.append(keep)
                self._size += len(keep)
            if len(keep) < len(data) and not self.over:
                self.over = True
                if self._on_over:
                    self._on_over()

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


def _signal(proc: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _stop_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM to the process group the server started; SIGKILL when it is
    still there after TERM_GRACE seconds, so no run blocks the next one."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        _signal(proc, sig)
        try:
            proc.wait(timeout=TERM_GRACE)
            return
        except subprocess.TimeoutExpired:
            continue


def _finish(proc: subprocess.Popen[bytes], *drains: _Drain) -> None:
    """Wait for both pipes to close. A process the child left behind that
    still holds them open is stopped with the rest of the group."""
    for d in drains:
        d.join(TERM_GRACE)
    if any(d.is_alive() for d in drains):
        _signal(proc, signal.SIGTERM)
        for d in drains:
            d.join(TERM_GRACE)
    if any(d.is_alive() for d in drains):
        _signal(proc, signal.SIGKILL)
        for d in drains:
            d.join(TERM_GRACE)
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


class Runner:
    """Cached, rate-limited access to the allowed commands."""

    def __init__(
        self,
        root: Path = config.FACTORY_ROOT,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        policies: Mapping[str, config.Policy] | None = None,
    ) -> None:
        self.root = root
        pol = dict(policies or config.COMMANDS)
        self._caches = {n: CommandCache(pol[n], clock, wall) for n in ALLOWED}
        self._timeouts = {n: pol[n].timeout for n in ALLOWED}

    def get(
        self,
        name: str,
        refresh: bool = False,
        session_id: Callable[[], str | None] | None = None,
    ) -> dict[str, Any]:
        """The cached result of `name`; a new run only when the cache and
        the minimum interval allow it. `session_id` is asked for the
        session only when a run starts; the result keeps it under
        `session` for the server, which shows only a shortened form."""
        if name not in ALLOWED:
            raise ValueError(f"command not allowed: {name!r}")

        def produce(launch: Callable[[], None]) -> dict[str, Any]:
            sid = session_id() if session_id else None
            launch()
            res = run(name, self.root, sid, self._timeouts[name])
            res["session"] = sid
            return res

        return self._caches[name].get(produce, refresh)
