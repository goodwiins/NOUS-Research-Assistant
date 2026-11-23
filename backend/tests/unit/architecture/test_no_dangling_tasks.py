"""No fire-and-forget ``create_task`` in backend/src (audit ST3, ruff RUF006).

The event loop holds only a weak reference to a task. A bare
``asyncio.create_task(coro)`` statement drops the only strong one, so the task
can be garbage-collected mid-run and its exception is never seen. Use
``src.core.background_tasks.spawn`` (or keep the task yourself).

Flags an expression *statement* whose value is a call to ``.create_task(...)``
or ``asyncio.ensure_future(...)``; assigning, awaiting or returning the task is
fine.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
DANGLING_CALLS = frozenset({"create_task", "ensure_future"})


def _violations(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
                continue
            func = node.value.func
            if isinstance(func, ast.Attribute) and func.attr in DANGLING_CALLS:
                found.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    return found


def test_no_dangling_create_task() -> None:
    bad = _violations(SRC)
    assert not bad, (
        "Task created without keeping a reference (GC can drop it, errors are "
        "lost). Use src.core.background_tasks.spawn:\n  " + "\n  ".join(bad)
    )


def test_detector_flags_only_dropped_tasks(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(
        "import asyncio\n"
        "async def f(c):\n"
        "    t = asyncio.create_task(c)\n"
        "    await asyncio.create_task(c)\n"
        "    asyncio.create_task(c)\n"
    )
    assert _violations(tmp_path) == ["mod.py:5"]
