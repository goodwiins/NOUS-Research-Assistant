"""Job and artifact names, and the one guard every file read goes through."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from . import config

READ_CAP = config.READ_CAP
CUT_NOTICE = "\n\n[cut: the file is larger than 2 MB; open it on disk]\n"

JOB_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{0,79}$")
KEY_RE = re.compile(
    r"^(?:spec|build|decision|decision-[1-9]|pr-body|related-tests"
    r"|round-[1-9]/review-[a-z]{1,12}|round-[1-9]/shadow-verify)$"
)


@dataclass(frozen=True)
class Read:
    text: str
    truncated: bool


def jobs_dir(root: Path) -> Path:
    return root / "jobs"


def listed_jobs(root: Path) -> list[str]:
    """Job folder names as os.listdir returns them: real directories (no
    symlink) whose name matches JOB_RE."""
    folder = jobs_dir(root)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    out = []
    for name in names:
        if not JOB_RE.fullmatch(name):
            continue
        try:
            st = os.lstat(folder / name)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            out.append(name)
    return sorted(out)


def listed_job(root: Path, job: str) -> str | None:
    """The listed folder name equal to `job`, or None. Paths are built from
    the returned name, never from the request text."""
    if not JOB_RE.fullmatch(job):
        return None
    for name in listed_jobs(root):
        if name == job:
            return name
    return None


def key_parts(key: str) -> tuple[str, ...] | None:
    """The relative file path of an artifact key, or None for any other key."""
    if not KEY_RE.fullmatch(key):
        return None
    parts = key.split("/")
    return (*parts[:-1], parts[-1] + ".md")


def _open_under(root: Path, path: Path) -> int | None:
    """A read-only descriptor for `path`, opened one component at a time from
    the root's descriptor with O_NOFOLLOW (directories also O_DIRECTORY), so
    no symlink anywhere below the root is followed, even one swapped in
    after a check. None when that fails."""
    try:
        parts = Path(os.path.abspath(path)).relative_to(os.path.abspath(root)).parts
    except ValueError:
        return None
    if not parts or any(p in ("", ".", "..") for p in parts):
        return None
    try:
        fd = os.open(os.path.realpath(root), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    except OSError:
        return None
    finally:
        os.close(fd)


def safe_read(root: Path, path: Path, tail: bool = False) -> Read | None:
    """Read a regular file under `root`, at most READ_CAP bytes (the last
    ones when `tail`). None when it is missing, outside the root, reached
    through a symlink or not a regular file."""
    fd = _open_under(root, path)
    if fd is None:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        truncated = st.st_size > READ_CAP
        if truncated and tail:
            os.lseek(fd, st.st_size - READ_CAP, os.SEEK_SET)
        chunks = []
        left = READ_CAP
        while left > 0:
            data = os.read(fd, min(left, 1 << 20))
            if not data:
                break
            chunks.append(data)
            left -= len(data)
        if left == 0 and os.read(fd, 1):
            truncated = True
    finally:
        os.close(fd)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    return Read(text=text, truncated=truncated)
