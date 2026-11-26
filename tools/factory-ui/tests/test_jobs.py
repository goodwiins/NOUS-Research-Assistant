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
    Factory,
    Running,
    start,
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


def test_odd_values_make_one_job_unreadable_not_the_list(
    factory: Factory,
) -> None:
    """NaN, Infinity or a number that overflows: that job shows unreadable,
    the healthy jobs and the Verifier still answer."""
    jobs = factory.root / "jobs"
    bad = {
        "2026-01-08-example-nan": '{"cache": {"phase": "build", "created": NaN}}',
        "2026-01-09-example-inf": '{"cache": {"phase": "build"}, "cache_at": 1e400}',
        "2026-01-10-example-bigint": '{"cache": {"phase": "build", "created": '
        + "9" * 400
        + "}}",
    }
    for job, raw in bad.items():
        (jobs / job).mkdir()
        (jobs / job / "job.json").write_text(raw)
    for running in start(factory):
        res = running.get("/api/jobs")
        assert res.status == 200
        rows = {r["id"]: r for r in res.json()["rows"]}
        assert rows["2026-01-08-example-nan"]["group"] == "unreadable"
        assert rows["2026-01-09-example-inf"]["group"] == "unreadable"
        assert rows["2026-01-10-example-bigint"]["group"] == "cached"
        assert rows["2026-01-10-example-bigint"]["age_s"] is None
        assert rows[ACTIVE]["group"] == "cached"
        assert b"NaN" not in res.body and b"Infinity" not in res.body
        assert running.get("/api/verifier").status == 200
        for job in bad:
            assert running.get(f"/api/jobs/{job}").status == 200


def test_failed_old_request_does_not_replace_the_new_view(
    running: Running, tmp_path: Path
) -> None:
    data = running.get("/api/parallel").json()
    out = render("stale-error", data, tmp_path)
    assert out == {"parallel": True, "error": False}


def test_deeply_nested_job_json_is_unreadable_not_a_500(factory: Factory) -> None:
    job = "2026-01-11-example-deep"
    (factory.root / "jobs" / job).mkdir()
    (factory.root / "jobs" / job / "job.json").write_text("[" * 200_000)
    for running in start(factory):
        res = running.get("/api/jobs")
        assert res.status == 200
        rows = {r["id"]: r for r in res.json()["rows"]}
        assert rows[job]["group"] == "unreadable"
        assert rows[ACTIVE]["group"] == "cached"
        assert running.get("/api/verifier").status == 200
        assert running.get(f"/api/jobs/{job}").status == 200


def test_cached_field_of_the_wrong_type_is_unreadable_not_the_view(
    factory: Factory, tmp_path: Path
) -> None:
    """A cached field the Jobs view prints that is an object or an array:
    that job shows unreadable, the healthy jobs still render."""
    jobs = factory.root / "jobs"
    bad = {
        "2026-01-12-example-phase-object": '{"cache": {"phase": {"toString": null}}}',
        "2026-01-13-example-hold-array": '{"cache": {"phase": "build", "hold": [1]}}',
        "2026-01-14-example-lease-host": '{"cache": {"phase": "build", '
        '"lease": {"host": {"toString": null}}}}',
    }
    for job, raw in bad.items():
        (jobs / job).mkdir()
        (jobs / job / "job.json").write_text(raw)
    for running in start(factory):
        data = running.get("/api/jobs").json()
        rows = {r["id"]: r for r in data["rows"]}
        for job in bad:
            assert rows[job] == {"id": job, "group": "unreadable"}
            assert running.get(f"/api/jobs/{job}").status == 200
        assert rows[ACTIVE]["group"] == "cached"
        text = render("jobs", data, tmp_path)["text"]
        for job in bad:
            assert job + ": unreadable" in text
        assert ACTIVE in text and "review" in text and "1/3" in text
