"""The verifier's files: `verify.mode` and `verify-backtest.jsonl`."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from .. import paths
from . import as_list

ROW_FIELDS = (
    "job",
    "verdict",
    "why",
    "flags",
    "flip",
    "overclaim",
    "revised",
    "expect",
    "match",
    "owner_verdict",
    "owner_note",
    "at",
    "proof",
)


def valid_date(text: str) -> bool:
    """The helper's valid_date: YYYY-MM-DD and a real calendar day."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text or ""):
        return False
    try:
        dt.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def verify_mode(root: Path) -> tuple[str, str | None]:
    """The helper's verify_mode: ("off", None), ("shadow", None) or
    ("gate", "YYYY-MM-DD"). An absent, unreadable or unknown file is off."""
    read = paths.safe_read(root, root / "verify.mode")
    if read is None:
        return ("off", None)
    words = read.text.split()
    if words in (["off"], ["shadow"]):
        return (words[0], None)
    if len(words) == 2 and words[0] == "gate" and valid_date(words[1]):
        return ("gate", words[1])
    return ("off", None)


def _lines(root: Path) -> list[str]:
    read = paths.safe_read(root, root / "verify-backtest.jsonl")
    return read.text.splitlines() if read else []


def count_backtests(root: Path) -> dict[str, Any]:
    """The `backtests` block of `F stats`, ported line by line from the
    helper's verify_stats over the same file, with its key names."""
    bt: dict[str, Any] = {
        "rows": 0,
        "verdict": {},
        "expected": 0,
        "match": 0,
        "flagged": 0,
        "flagged_on_expected_pass": 0,
        "no_match": 0,
        "owner_read": 0,
    }
    for line in _lines(root):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        bt["rows"] += 1
        v = str(r.get("verdict") or "-")
        bt["verdict"][v] = bt["verdict"].get(v, 0) + 1
        if r.get("expect"):
            bt["expected"] += 1
            m = r.get("match")
            bt[
                (
                    "flagged"
                    if m == "flagged"
                    else (
                        "flagged_on_expected_pass"
                        if m == "flagged-pass"
                        else "match" if m is True else "no_match"
                    )
                )
            ] += 1
        if r.get("owner_verdict"):
            bt["owner_read"] += 1
    return bt


def backtest_rows(root: Path) -> dict[str, Any]:
    """The backtest rows with the shown fields; a line that is not a JSON
    object is skipped and counted."""
    rows, skipped = [], 0
    for line in _lines(root):
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
        rows.append({k: r.get(k) for k in ROW_FIELDS})
    return {"rows": rows, "skipped": skipped}


def round_rows(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per verify run recorded in a cached job's verify_runs or
    verify_shadow."""
    out = []
    for job in jobs:
        for field in ("verify_runs", "verify_shadow"):
            entries = job.get(field)
            if not isinstance(entries, dict):
                continue
            for rnd, e in sorted(entries.items()):
                if not isinstance(e, dict) or not str(rnd).isdigit():
                    continue
                flags = as_list(e.get("flags"))
                out.append(
                    {
                        "job": job.get("id"),
                        "round": str(rnd),
                        "field": field,
                        "mode": e.get("mode"),
                        "verdict": e.get("verdict"),
                        "why": e.get("why"),
                        "diff_finding": "diff-finding" in flags,
                    }
                )
    return out
