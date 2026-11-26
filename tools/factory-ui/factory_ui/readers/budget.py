"""Usage readings (`usage/<session id>.json`) and the `F budget` answer.

A session id is shown by its first 8 characters at most, and never more than
half of it (short()); the full id goes only into the `budget` child's
environment.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .. import config, paths
from . import loads

_READING = re.compile(r"^[A-Za-z0-9_-]{1,80}\.json$")
WINDOWS = tuple(config.LIMITS)
ANSWER_FIELDS = (
    "go",
    "why",
    "wake_at",
    "stale",
    "reading",
    "limits",
    "limits_source",
    "limits_error",
)


def _window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    pct, resets = value.get("used_percentage"), value.get("resets_at")
    return {
        "used_percentage": pct if isinstance(pct, (int, float)) else None,
        "resets_at": resets if isinstance(resets, (int, float)) else None,
    }


def _readings(root: Path) -> list[tuple[str, dict[str, Any]]]:
    """(session id, reading) for every readable reading, newest first."""
    folder = root / "usage"
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for name in names:
        if not _READING.fullmatch(name):
            continue
        read = paths.safe_read(root, folder / name)
        if read is None:
            continue
        try:
            data = loads(read.text)
            seen = int(data.get("seen_at") or 0)
            limits = data.get("rate_limits") or {}
        except (ValueError, AttributeError, TypeError):
            continue
        if not isinstance(limits, dict):
            continue
        windows = {w: _window(limits.get(w)) for w in WINDOWS}
        out.append((name[: -len(".json")], {"seen_at": seen, "windows": windows}))
    out.sort(key=lambda x: int(x[1]["seen_at"]), reverse=True)
    return out


def short(sid: str) -> str:
    """The shown part of a session id: the first 8 characters, at most half
    of the id, so a short id is never shown whole."""
    return sid[: min(8, len(sid) // 2)]


def newest_session(root: Path) -> str | None:
    """The stem of the newest reading by seen_at, from os.listdir."""
    readings = _readings(root)
    return readings[0][0] if readings else None


def recent_readings(root: Path, now: float) -> list[dict[str, Any]]:
    """Readings of the last 6 hours, each with its session shortened."""
    out = []
    for sid, r in _readings(root):
        age = now - r["seen_at"]
        if age > config.USAGE_TRUST:
            continue
        out.append(
            {
                "session": short(sid),
                "seen_at": r["seen_at"],
                "stale": age > config.USAGE_MAX_AGE,
                "windows": r["windows"],
            }
        )
    return out


_PATH = re.compile(r"(?<![\w.])/[^\s'\"]+")  # an absolute path


def scrub(value: Any, sid: str | None) -> Any:
    """A helper diagnostic without the full session id or a file path (an
    OSError names the usage file, whose name is the session id)."""
    if not isinstance(value, str):
        return value
    if sid:
        value = value.replace(sid, short(sid) + "...")
    return _PATH.sub("<path>", value)


def answer(data: dict[str, Any] | None, sid: str | None) -> dict[str, Any] | None:
    """The `F budget` answer, field by field. `sid`: the session id the run
    used, removed from every text field."""
    if data is None:
        return None
    out = {k: scrub(data[k], sid) for k in ANSWER_FIELDS if k in data}
    usage = data.get("usage")
    if isinstance(usage, dict):
        out["usage"] = {w: _window(usage.get(w)) for w in WINDOWS}
    return out
