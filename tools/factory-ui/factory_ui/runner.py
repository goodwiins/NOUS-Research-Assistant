"""The only module that starts a process: two fixed helper commands.

Both are read-only in the helper's code (see README.md, "Allowlist"). The
command name comes from the server's route table, never from request text,
and run() refuses any other name before it spawns anything.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import config
from .cache import CommandCache

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
        # stdout and stderr as pipes: capture_output=True, with Popen so a
        # timeout can stop the whole process group the child leads.
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
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_group(proc)
        out["status"] = "timeout"
        out["error"] = f"no answer within {timeout:g} s"
        return out
    out["exit"] = proc.returncode
    out["stderr"] = stderr.decode("utf-8", errors="replace")[: config.STDERR_CAP]
    if len(stdout) > config.STDOUT_CAP:
        out["error"] = "output larger than 1 MB"
        return out
    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    except ValueError:
        out["error"] = "output is not JSON"
        return out
    if not isinstance(data, dict):
        out["error"] = "output is not a JSON object"
        return out
    out["data"] = data
    out["status"] = _status(proc.returncode)
    return out


def _stop_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM to the process group the server started; SIGKILL when it is
    still there after TERM_GRACE seconds, so no run blocks the next one."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=TERM_GRACE)
            return
        except subprocess.TimeoutExpired:
            continue


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
        session only when a run starts."""
        if name not in ALLOWED:
            raise ValueError(f"command not allowed: {name!r}")

        def produce() -> dict[str, Any]:
            sid = session_id() if session_id else None
            res = run(name, self.root, sid, self._timeouts[name])
            res["session"] = sid[:8] if sid else None
            return res

        return self._caches[name].get(produce, refresh)
