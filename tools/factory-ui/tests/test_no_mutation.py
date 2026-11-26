"""Acceptance criterion 5: the server changes nothing in the factory tree."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from conftest import ALL_ROUTES, Factory, start

WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
WRITE_EVENTS = {
    "os.rename",
    "os.replace",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "os.mkdir",
    "os.chmod",
    "os.chown",
    "os.utime",
    "os.symlink",
    "os.link",
    "os.truncate",
    "shutil.copyfile",
    "shutil.copymode",
    "shutil.copystat",
    "shutil.move",
    "shutil.rmtree",
}


class Audit:
    """An audit hook stays installed for the life of the process, so it only
    records while `active` is set."""

    active = False
    root = ""
    hits: list[tuple[str, str]] = []

    @classmethod
    def under_root(cls, path: Any) -> str | None:
        if isinstance(path, int) or path is None:
            return None
        p = os.fsdecode(path)
        full = os.path.realpath(p)
        if full == cls.root or full.startswith(cls.root + os.sep):
            return full
        return None

    @classmethod
    def hook(cls, event: str, args: tuple) -> None:
        if not cls.active:
            return
        if event == "open":
            path, mode, flags = args
            writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & WRITE_FLAGS
            )
            hit = cls.under_root(path) if writes else None
            if hit:
                cls.hits.append((event, hit))
        elif event in WRITE_EVENTS:
            for a in args[:2]:
                if isinstance(a, (str, bytes, os.PathLike)):
                    hit = cls.under_root(a)
                    if hit:
                        cls.hits.append((event, hit))


_installed = False


def _install() -> None:
    global _installed
    if not _installed:
        sys.addaudithook(Audit.hook)
        _installed = True


def snapshot(root: Path) -> list[tuple]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            out.append((p, st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode))
    return sorted(out)


def test_no_route_changes_the_tree(factory: Factory) -> None:
    _install()
    before = snapshot(factory.root)
    Audit.root = os.path.realpath(factory.root)
    Audit.hits = []
    Audit.active = True
    try:
        for running in start(factory):
            for _ in range(2):
                for path in ALL_ROUTES:
                    assert running.get(path).status == 200, path
                running.get("/api/health?refresh=1")
                running.get("/api/budget?refresh=1")
    finally:
        Audit.active = False
    assert Audit.hits == []
    assert snapshot(factory.root) == before


def test_audit_hook_sees_a_write(factory: Factory) -> None:
    """The hook itself works: a deliberate write under the root is caught."""
    _install()
    Audit.root = os.path.realpath(factory.root)
    Audit.hits = []
    Audit.active = True
    try:
        (factory.root / "canary-write.txt").write_text("x")
        os.remove(factory.root / "canary-write.txt")
    finally:
        Audit.active = False
    assert {e for e, _ in Audit.hits} == {"open", "os.remove"}
