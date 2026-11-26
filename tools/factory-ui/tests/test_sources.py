"""Acceptance criterion 10: the switchable sources."""

from __future__ import annotations

import re
from typing import Any

import pytest
from conftest import PKG_ROOT, Running
from factory_ui import sources
from factory_ui.readers import jobs as jobs_reader
from factory_ui.readers import verifier as verifier_reader

CANNED_ROW = {
    "id": "2026-01-20-canned-row",
    "phase": "build",
    "round": 1,
    "pr": None,
    "home": "mac-b",
    "lease": None,
    "group": "cached",
    "verify_runs": {"1": {"verdict": "PASS", "why": "flip", "mode": "gate"}},
}
CANNED_BLOCK = {
    "rows": 99,
    "verdict": {"PASS": 99},
    "expected": 98,
    "match": 97,
    "flagged": 0,
    "flagged_on_expected_pass": 1,
    "no_match": 0,
    "owner_read": 3,
}


def test_routes_use_only_the_sources(
    running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("a file reader was called")

    monkeypatch.setattr(sources, "jobs_rows", lambda root: [dict(CANNED_ROW)])
    monkeypatch.setattr(sources, "backtests_block", lambda root: dict(CANNED_BLOCK))
    monkeypatch.setattr(jobs_reader, "read_jobs", boom)
    monkeypatch.setattr(verifier_reader, "count_backtests", boom)
    jobs = running.get("/api/jobs")
    assert jobs.status == 200
    assert jobs.json()["rows"] == [CANNED_ROW]
    ver = running.get("/api/verifier")
    assert ver.status == 200
    body = ver.json()
    assert body["backtests"] == CANNED_BLOCK
    # The per-round table is built from the source rows too.
    assert body["rounds"] == [
        {
            "job": "2026-01-20-canned-row",
            "round": "1",
            "field": "verify_runs",
            "mode": "gate",
            "verdict": "PASS",
            "why": "flip",
            "diff_finding": False,
        }
    ]


def test_sources_default_to_the_file_readers(running: Running) -> None:
    root = running.factory.root
    now = 2_000_000_000.0
    assert sources.jobs_rows(root, now=now) == jobs_reader.read_jobs(root, now=now)
    assert sources.backtests_block(root) == verifier_reader.count_backtests(root)


def test_runner_names_no_list_stats_or_show() -> None:
    text = (PKG_ROOT / "factory_ui" / "runner.py").read_text()
    assert not re.search(r'"(list|stats|show)"', text)
