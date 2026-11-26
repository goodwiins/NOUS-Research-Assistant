"""Acceptance criterion 9: the Jobs view."""

from __future__ import annotations

from pathlib import Path

from conftest import (
    ACTIVE,
    BROKEN,
    ESCALATED,
    LINKED,
    MERGED,
    NO_CACHE,
    PR_OPEN,
    Running,
)
from dom import render

EXPECTED = {
    ACTIVE: ("review", 1, 3, None, "mac-a"),
    PR_OPEN: ("pr-open", 2, 3, 42, "mac-b"),
    ESCALATED: ("escalated", 1, 3, None, "mac-a"),
    MERGED: ("merged", 1, 3, 41, "mac-b"),
}


def test_api_jobs_rows(running: Running) -> None:
    data = running.get("/api/jobs").json()
    rows = {r["id"]: r for r in data["rows"]}
    assert set(rows) == set(EXPECTED) | {NO_CACHE, BROKEN}
    assert LINKED not in rows
    for job, (phase, rnd, max_rounds, pr, home) in EXPECTED.items():
        r = rows[job]
        assert r["group"] == "cached"
        assert (r["phase"], r["round"], r["max_rounds"], r["pr"], r["home"]) == (
            phase,
            rnd,
            max_rounds,
            pr,
            home,
        )
        assert isinstance(r["age_s"], (int, float)) and r["age_s"] >= 0
        assert isinstance(r["cache_age_s"], (int, float)) and r["cache_age_s"] >= 0
        assert r["linear"].startswith("GOO-")
    # Age: now minus created, or minus finished for a finished job.
    assert 2 * 3600 - 60 <= rows[ACTIVE]["age_s"] <= 2 * 3600 + 60
    assert 9 * 86400 - 60 <= rows[MERGED]["age_s"] <= 9 * 86400 + 60
    assert 300 - 60 <= rows[ACTIVE]["cache_age_s"] <= 300 + 60
    assert rows[ACTIVE]["lease"]["host"] == "mac-a"
    assert 60 <= rows[ACTIVE]["lease_beat_age_s"] <= 180
    assert rows[ESCALATED]["kind"] == "POLICY"
    assert rows[ESCALATED]["hold"] == "waiting on the owner"
    assert rows[PR_OPEN]["title"] == "Example PR title"
    assert rows[ACTIVE]["title"] == rows[ACTIVE]["title"][:100]
    assert len(rows[ACTIVE]["title"]) == 100
    assert rows[NO_CACHE]["group"] == "no-cache"
    assert rows[BROKEN]["group"] == "unreadable"


def test_rendered_jobs_view(running: Running, tmp_path: Path) -> None:
    data = running.get("/api/jobs").json()
    out = render("jobs", data, tmp_path)
    text = out["text"]
    assert "this Mac's cache" in text
    for job in EXPECTED:
        assert job in text
    for cell in ("review", "pr-open", "escalated", "merged", "POLICY"):
        assert cell in text
    assert "1/3" in text and "2/3" in text
    assert "#42" in text and "#41" in text
    assert "mac-a" in text and "mac-b" in text
    assert "from cache" in text
    html = out["html"]
    assert "not in cache" in text
    assert html.index("not in cache") < html.index(NO_CACHE)
    assert "unreadable" in text and BROKEN in text
    # Ages are shown as durations, PR "-" when absent.
    assert "2h" in text and "9d" in text and "5m" in text
    assert ">-<" in html
