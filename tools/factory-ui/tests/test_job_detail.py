"""Acceptance criterion 11: job detail, artifacts, rendering and secrets."""

from __future__ import annotations

from pathlib import Path

from conftest import (
    ACTIVE,
    ALL_ROUTES,
    ARTIFACT_KEYS,
    DOCS_VALUE,
    ESCALATED,
    LEASE_SESSION,
    PR_OPEN,
    RERUN_MARKER,
    WRITE_TOKEN,
    XSS_SPEC,
    Factory,
    Running,
    start,
)
from dom import render
from factory_ui.readers import reviews


def test_each_artifact_is_returned(running: Running) -> None:
    root = running.factory.root
    for job, keys in ARTIFACT_KEYS.items():
        detail = running.get(f"/api/jobs/{job}").json()
        present = {a["key"] for a in detail["artifacts"] if a["present"]}
        assert present == set(keys), job
        for key in keys:
            res = running.get(f"/api/jobs/{job}/artifact/{key}")
            assert res.status == 200, (job, key)
            data = res.json()
            assert data["key"] == key
            parts = key.split("/")
            disk = (root / "jobs" / job / "/".join(parts)).with_suffix(".md")
            on_disk = disk.read_text()
            if "## Re-run" not in on_disk and not data["truncated"]:
                assert data["text"] == on_disk
    missing = running.get(f"/api/jobs/{ACTIVE}").json()["artifacts"]
    assert {"key": "decision", "present": False} in missing


def test_spec_xss_is_inert_rendered_and_raw(running: Running, tmp_path: Path) -> None:
    text = running.get(f"/api/jobs/{ACTIVE}/artifact/spec").json()["text"]
    assert text == XSS_SPEC
    for view in ("markdown", "raw"):
        out = render(view, {"text": text}, tmp_path)
        assert "script" not in out["tags"] and "img" not in out["tags"]
        assert all(not h.lower().startswith("javascript:") for h in out["hrefs"])
        assert "<script>" not in out["html"] and "<img" not in out["html"]
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out["html"]
        assert "onerror=alert(2)" in out["text"]
    md = render("markdown", {"text": text}, tmp_path)
    assert md["hrefs"] == ["https://example.org/x"]
    assert "javascript:alert(3)" in md["text"] or "bad link" in md["text"]
    for tag in ("h1", "table", "pre", "code"):
        assert tag in md["tags"]


def test_verdict_table_marks_mismatch_and_diff_finding(running: Running) -> None:
    detail = running.get(f"/api/jobs/{ACTIVE}").json()
    (r1,) = detail["rounds"]
    lenses = {x["lens"]: x for x in r1["reviews"]}
    assert lenses["code"]["state"] == "PASS" and lenses["code"]["file"] == "PASS"
    assert lenses["code"]["mismatch"] is False
    assert lenses["security"]["state"] == "CHANGES"
    assert lenses["security"]["file"] == "PASS"
    assert lenses["security"]["mismatch"] is True
    assert lenses["xmodel"]["state"] is None
    v = r1["verify"]
    assert v["field"] == "verify_runs"
    assert (v["verdict"], v["why"], v["mode"]) == ("CHANGES", "flip", "gate")
    assert v["diff_finding"] is True
    other = running.get(f"/api/jobs/{PR_OPEN}").json()
    shadow = {r["round"]: r for r in other["rounds"]}["1"]["verify"]
    assert shadow["field"] == "verify_shadow"
    assert shadow["diff_finding"] is False


def test_diff_finding_from_file_flag_alone(running: Running) -> None:
    text = running.get(f"/api/jobs/{ACTIVE}/artifact/round-1/review-verify").json()
    assert reviews.verify_header(text["text"])["FLAG"] == ["diff-finding"]


def test_rendered_job_view_shows_marker_and_mismatch(
    running: Running, tmp_path: Path
) -> None:
    detail = running.get(f"/api/jobs/{ACTIVE}").json()
    out = render(
        "job", {"detail": detail, "texts": {}, "open": [], "raw": []}, tmp_path
    )
    assert "diff-finding" in out["text"]
    assert "mismatch" in out["text"]
    assert "not written yet" in out["text"]


def test_rerun_body_is_hidden_everywhere(running: Running) -> None:
    res = running.get(f"/api/jobs/{ACTIVE}/artifact/round-1/review-verify").json()
    assert reviews.HIDDEN_NOTICE in res["text"]
    assert "## Notes" in res["text"] and "After the hidden section." in res["text"]
    secrets = (RERUN_MARKER, WRITE_TOKEN, LEASE_SESSION, DOCS_VALUE)
    for path in ALL_ROUTES + ["/api/health?refresh=1", "/api/budget?refresh=1"]:
        body = running.get(path).body
        for s in secrets:
            assert s.encode() not in body, (path, s)


def test_hide_rerun_unit() -> None:
    text = "# T\n\n## Re-run\n\nA=1\nB=2\n\n### sub\nC=3\n## Next\nkeep\n"
    out = reviews.hide_rerun(text)
    assert "A=1" not in out and "C=3" not in out
    assert out.endswith("## Next\nkeep\n")
    assert reviews.hide_rerun("## Re-run\nX=1") == "## Re-run\n\n" + (
        reviews.HIDDEN_NOTICE + "\n"
    )
    assert reviews.hide_rerun("no section\n") == "no section\n"


def test_summary_omits_secret_fields(running: Running) -> None:
    detail = running.get(f"/api/jobs/{ACTIVE}").json()
    summary = detail["summary"]
    assert "write_token" not in summary["local"]
    assert "write_token" not in summary["cache"]
    assert "docs" not in summary["cache"]
    assert "session" not in summary["cache"]["lease"]
    assert summary["cache"]["lease"]["host"] == "mac-a"


def test_file_only_verify_round_uses_the_file_header(
    factory: Factory, tmp_path: Path
) -> None:
    """A verify file with no recorded entry: verdict, why and mode come from
    the file's header lines."""
    folder = factory.root / "jobs" / ESCALATED
    (folder / "round-3").mkdir()
    (folder / "round-3" / "shadow-verify.md").write_text(
        "VERDICT: CHANGES\nHEAD: x\nBASE: y\nMODE: shadow\nWHY: flip\n\n## Notes\n"
    )
    for running in start(factory):
        detail = running.get(f"/api/jobs/{ESCALATED}").json()
    v = {r["round"]: r for r in detail["rounds"]}["3"]["verify"]
    assert v["field"] is None
    assert (v["verdict"], v["why"], v["mode"]) == ("CHANGES", "flip", "shadow")
    assert v["key"] == "round-3/shadow-verify"
    assert v["diff_finding"] is False


def test_late_artifact_response_does_not_replace_another_view(
    running: Running, tmp_path: Path
) -> None:
    detail = running.get(f"/api/jobs/{ACTIVE}").json()
    out = render("stale-draw", {"detail": detail}, tmp_path)
    assert out == {"elsewhere": 0, "onPage": 1}


def test_rerun_with_tab_heading_is_hidden_on_both_routes(factory: Factory) -> None:
    """`##<tab>Re-run`: the artifact route and the job-detail verify row see
    the same section boundary, so no value from the hidden body escapes."""
    marker = "TAB-RERUN-MARKER-81c3"
    folder = factory.root / "jobs" / ESCALATED
    (folder / "round-4").mkdir()
    (folder / "round-4" / "shadow-verify.md").write_text(
        "VERDICT: PASS\nHEAD: x\nBASE: y\nMODE: shadow\n"
        f"##\tRe-run\nWHY: {marker}\nTOKEN={marker}\n##\tNotes\nafter\n"
    )
    for running in start(factory):
        detail = running.get(f"/api/jobs/{ESCALATED}")
        art = running.get(f"/api/jobs/{ESCALATED}/artifact/round-4/shadow-verify")
    assert marker.encode() not in detail.body
    assert marker.encode() not in art.body
    v = {r["round"]: r for r in detail.json()["rounds"]}["4"]["verify"]
    assert (v["verdict"], v["mode"], v["why"]) == ("PASS", "shadow", None)
    assert reviews.HIDDEN_NOTICE in art.json()["text"]
    assert "after" in art.json()["text"]
