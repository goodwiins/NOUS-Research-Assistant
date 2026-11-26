"""The one place the server gets the Jobs rows, the backtests block and the
backtest rows.

v1 reads files. `F list` and `F stats` write under the factory directory
(both call the helper's write_targets), so they are not run. Once the helper
gains a no-write mode, these two functions can call it instead, and the
command joins runner.ALLOWED with a new proof from the helper code; the
server and the frontend use only the shapes returned here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .readers import jobs, verifier


def jobs_rows(root: Path, now: float | None = None) -> list[dict[str, Any]]:
    """One row per job with the `F list` fields under its names (id, phase,
    round, pr, home, lease) plus the UI's extra fields."""
    return jobs.read_jobs(root, now=now)


def backtests_block(root: Path) -> dict[str, Any]:
    """The `F stats` verify.backtests block, under its key names."""
    return verifier.count_backtests(root)


def backtest_rows(root: Path) -> dict[str, Any]:
    """The rows of `verify-backtest.jsonl` the Verifier view lists:
    {"rows", "skipped", "truncated"}. `F stats` does not print rows, so this
    stays a file read after the switch."""
    return verifier.backtest_rows(root)
