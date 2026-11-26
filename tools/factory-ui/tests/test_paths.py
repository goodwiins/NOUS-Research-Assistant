"""Acceptance criterion 8: paths, symlinks and the size cap."""

from __future__ import annotations

import pytest
from conftest import ACTIVE, LINKED, MERGED, OUTSIDE_BYTES, Running
from factory_ui import paths

BAD = [
    f"/api/jobs/{ACTIVE}/artifact/../job",
    f"/api/jobs/{ACTIVE}/artifact/..%2fjob",
    f"/api/jobs/{ACTIVE}/artifact/%2e%2e/job",
    f"/api/jobs/{ACTIVE}/artifact/%252e%252e%252fjob",
    f"/api/jobs/{ACTIVE}/artifact/round-1%2freview-code",
    f"/api/jobs/{ACTIVE}/artifact/spec%00",
    f"/api/jobs/{ACTIVE}/artifact/spec\x00.md",
    f"/api/jobs/{ACTIVE}/artifact//etc/passwd",
    "/api/jobs/%2e%2e/artifact/spec",
    "/api/jobs/..%2f..%2foutside/artifact/spec",
    "/api/jobs//etc/artifact/spec",
    "/api/jobs/2026-01-09-not-listed",
    "/api/jobs/2026-01-09-not-listed/artifact/spec",
    f"/api/jobs/{ACTIVE}/artifact/answers",
    f"/api/jobs/{ACTIVE}/artifact/rework-note",
    f"/api/jobs/{ACTIVE}/artifact/scratch/x",
    f"/api/jobs/{ACTIVE}/artifact/xmodel-raw.json",
    f"/api/jobs/{ACTIVE}/artifact/xmodel-raw",
    f"/api/jobs/{ACTIVE}/artifact/job.json",
    f"/api/jobs/{ACTIVE}/artifact/job",
    f"/api/jobs/{ACTIVE}/artifact/round-0/review-code",
    f"/api/jobs/{ACTIVE}/artifact/round-1/review-CODE",
    f"/api/jobs/{ACTIVE}/artifact/round-1/review-toolonglensname",
    f"/api/jobs/{ACTIVE}/artifact/decision-10",
    # A symlink in the job folder pointing outside the root.
    f"/api/jobs/{ACTIVE}/artifact/pr-body",
    # A symlinked job folder.
    f"/api/jobs/{LINKED}",
    f"/api/jobs/{LINKED}/artifact/spec",
    "/static/../factory_ui/config.py",
    "/static/%2e%2e/factory_ui/config.py",
    "/static/",
    "/static/app.js/",
]


def test_bad_paths_answer_400_or_404(running: Running) -> None:
    for path in BAD:
        res = running.get(path)
        assert res.status in (400, 404), path
        assert OUTSIDE_BYTES.encode() not in res.body, path


def test_outside_bytes_appear_nowhere(running: Running) -> None:
    for path in ["/api/jobs", "/api/verifier", f"/api/jobs/{ACTIVE}"]:
        res = running.get(path)
        assert res.status == 200
        assert OUTSIDE_BYTES.encode() not in res.body
        assert LINKED.encode() not in res.body


def test_oversize_file_is_cut_with_notice(running: Running) -> None:
    res = running.get(f"/api/jobs/{MERGED}/artifact/build")
    assert res.status == 200
    data = res.json()
    assert data["truncated"] is True
    assert paths.CUT_NOTICE in data["text"]
    assert len(data["text"].encode()) <= paths.READ_CAP + len(paths.CUT_NOTICE) + 8


@pytest.mark.parametrize(
    "key,expected",
    [
        ("spec", ("spec.md",)),
        ("decision-3", ("decision-3.md",)),
        ("round-2/review-security", ("round-2", "review-security.md")),
        ("round-9/shadow-verify", ("round-9", "shadow-verify.md")),
        ("round-1/shadow-code", None),
        ("spec.md", None),
        ("SPEC", None),
        ("../spec", None),
    ],
)
def test_key_mapping(key: str, expected: tuple[str, ...] | None) -> None:
    assert paths.key_parts(key) == expected


def test_job_pattern() -> None:
    assert paths.JOB_RE.fullmatch("2026-01-01-example-job")
    for bad in ("2026-01-01-", "2026-01-01-Example", "../2026-01-01-x", "x"):
        assert not paths.JOB_RE.fullmatch(bad)
