"""The verifier's files: `verify.mode` and `verify-backtest.jsonl`."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from .. import paths
from . import as_list, loads

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


def _read(root: Path) -> paths.Read | None:
    return paths.safe_read(root, root / "verify-backtest.jsonl")


def backtests(root: Path) -> dict[str, Any]:
    """The backtests block (F stats key names) recomputed from
    `verify-backtest.jsonl`, plus the UI's own `listing` key with the rows
    the Verifier view lists. One read serves both."""
    read = _read(root)
    lines = read.text.splitlines() if read else []
    block = _count(lines)
    block["listing"] = _rows(lines, bool(read and read.truncated))
    return block


def count_backtests(root: Path) -> dict[str, Any]:
    """The `backtests` block alone, under the F stats key names."""
    read = _read(root)
    return _count(read.text.splitlines() if read else [])


def _count(lines: list[str]) -> dict[str, Any]:
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
    for line in lines:
        try:
            r = loads(line)
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


def _rows(lines: list[str], truncated: bool) -> dict[str, Any]:
    """The backtest rows with the shown fields; a line that is not a JSON
    object is skipped and counted. `truncated`: the file is over the 2 MB
    read cap, so rows and the recomputed block cover its start only."""
    rows, skipped = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            r = loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(r, dict):
            skipped += 1
            continue
        rows.append({k: r.get(k) for k in ROW_FIELDS})
    return {
        "rows": rows,
        "skipped": skipped,
        "truncated": truncated,
    }


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
