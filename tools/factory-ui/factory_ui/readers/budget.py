"""Usage readings (`usage/<session id>.json`) and the `F budget` answer.

A session id is shown by its first 8 characters only; the full id goes only
into the `budget` child's environment.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .. import config, paths

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
            data = json.loads(read.text)
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
                "session": sid[:8],
                "seen_at": r["seen_at"],
                "stale": age > config.USAGE_MAX_AGE,
                "windows": r["windows"],
            }
        )
    return out


def answer(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """The `F budget` answer, field by field."""
    if data is None:
        return None
    out = {k: data[k] for k in ANSWER_FIELDS if k in data}
    usage = data.get("usage")
    if isinstance(usage, dict):
        out["usage"] = {w: _window(usage.get(w)) for w in WINDOWS}
    return out
