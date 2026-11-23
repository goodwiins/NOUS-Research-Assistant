"""Stdlib loggers must not get structlog-style keyword arguments (audit API-1).

``logging.Logger.info("event", thread_id=...)`` raises ``TypeError`` at call
time: the stdlib logger only accepts ``exc_info``, ``stack_info``,
``stacklevel`` and ``extra``. That one log line turned ``POST /api/v2/threads``
with a ``project_id`` into a guaranteed 500 (and IS-4 hid the same crash in the
arXiv change tracker). Use ``%s`` arguments or ``extra={...}`` instead, or a
``structlog`` logger if you want key/value events.

A module is checked when it binds a name with ``<name> = logging.getLogger(...)``;
every ``<name>.<level>(...)`` call in it must use only the allowed keywords.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
ALLOWED_KWARGS = frozenset({"exc_info", "stack_info", "stacklevel", "extra"})
LOG_METHODS = frozenset(
    {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "fatal",
        "log",
    }
)


def _stdlib_logger_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        func = node.value.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "getLogger"
            and isinstance(func.value, ast.Name)
            and func.value.id == "logging"
        ):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _violations(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        loggers = _stdlib_logger_names(tree)
        if not loggers:
            continue
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LOG_METHODS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in loggers
            ):
                continue
            bad = sorted(
                kw.arg
                for kw in node.keywords
                if kw.arg and kw.arg not in ALLOWED_KWARGS
            )
            if bad:
                rel = path.relative_to(root).as_posix()
                found.append(f"{rel}:{node.lineno} {', '.join(bad)}")
    return found


def test_stdlib_loggers_get_no_structlog_kwargs() -> None:
    bad = _violations(SRC)
    assert not bad, (
        "stdlib logging calls with keyword arguments that raise TypeError at "
        "runtime. Use %s args or extra={...}:\n  " + "\n  ".join(bad)
    )


def test_detector_flags_a_structlog_style_call(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "logger.info('ok %s', 1, exc_info=True)\n"
        "logger.info('event', thread_id='t')\n"
    )
    assert _violations(tmp_path) == ["mod.py:4 thread_id"]
