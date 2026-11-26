"""Parallel jobs, a placeholder: `parallel.mode` and `parallel-log.jsonl`
(PARALLEL-SPEC section 8). `F slot` and `F parallel` are never run."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .. import config, paths
from .verifier import valid_date

LOG_KEYS = ("at", "host", "mode", "kind", "job")
OFF: dict[str, Any] = {"mode": "off", "slots": None, "since": None}


def parse_mode(text: str) -> dict[str, Any]:
    """`off`, `shadow` or `on <slots> <YYYY-MM-DD>`; anything else is off."""
    words = text.split()
    if words in (["off"], ["shadow"]):
        return {"mode": words[0], "slots": None, "since": None}
    if (
        len(words) == 3
        and words[0] == "on"
        and re.fullmatch(r"[1-9][0-9]{0,2}", words[1])
        and valid_date(words[2])
    ):
        return {"mode": "on", "slots": int(words[1]), "since": words[2]}
    return dict(OFF)


def view(root: Path) -> dict[str, Any]:
    mode_read = paths.safe_read(root, root / "parallel.mode")
    log_read = paths.safe_read(root, root / "parallel-log.jsonl", tail=True)
    mode = parse_mode(mode_read.text) if mode_read else dict(OFF)
    if log_read is None:
        state = "mode" if mode_read else "dormant"
        return {"state": state, "mode": mode, "rows": [], "skipped": 0}
    lines = log_read.text.splitlines()
    if log_read.truncated and lines:
        lines = lines[1:]  # the cut first line
    rows, skipped = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(r, dict):
            skipped += 1
            continue
        row = {k: r.get(k) for k in LOG_KEYS}
        row["extra"] = {k: v for k, v in r.items() if k not in LOG_KEYS}
        rows.append(row)
    return {
        "state": "lit",
        "mode": mode,
        "rows": rows[-config.LOG_ROWS :],
        "skipped": skipped,
    }
