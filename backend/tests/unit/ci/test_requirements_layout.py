"""Backend requirements layout: one production set, locked, no test tools."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND = REPO_ROOT / "backend"
REQUIREMENTS = BACKEND / "requirements.txt"
LOCK = BACKEND / "requirements-cpu-lock.txt"

FORBIDDEN = {
    "testcontainers",
    "memory-profiler",
    "py-spy",
    "passlib",
    "tabula-py",
    "sse-starlette",
}


def _normalise(name: str) -> str:
    return re.sub(r"[_.]", "-", name.lower())


def _package_names(path: Path) -> list[str]:
    names = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        assert match, f"unparseable requirement in {path.name}: {raw!r}"
        names.append(_normalise(match.group(0)))
    return names


def test_production_requirements_hold_no_test_or_unused_tools() -> None:
    names = _package_names(REQUIREMENTS)
    assert not [n for n in names if n.startswith("pytest")]
    assert not FORBIDDEN.intersection(names)


def test_production_requirements_name_each_package_once() -> None:
    names = _package_names(REQUIREMENTS)
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert duplicates == []


def test_every_production_package_is_locked() -> None:
    locked = set(_package_names(LOCK))
    missing = sorted(set(_package_names(REQUIREMENTS)) - locked)
    assert missing == []


def test_orphan_requirements_files_are_gone() -> None:
    assert not (BACKEND / "requirements-test.txt").exists()
    assert not (BACKEND / "requirements-observability.txt").exists()
