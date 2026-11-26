"""Acceptance criterion 13: Budget and Health views."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import (
    BUDGET_GO,
    BUDGET_NO_DATA,
    BUDGET_WAIT,
    DOCTOR_LINEAR_DOWN,
    DOCTOR_OK,
    NEWEST_SESSION,
    SESSIONS,
    TMP_SESSION,
    Factory,
    Running,
    start,
)
from dom import render
from factory_ui import config


@pytest.mark.parametrize(
    "out,code,go",
    [(BUDGET_GO, 0, True), (BUDGET_WAIT, 1, False), (BUDGET_NO_DATA, 0, True)],
)
def test_budget_shapes_render_as_answers(
    factory: Factory, tmp_path: Path, out: dict, code: int, go: bool
) -> None:
    factory.set_control("budget", out=out, exit=code)
    for running in start(factory):
        data = running.get("/api/budget").json()
    assert data["command"]["status"] == "answer"
    assert data["command"]["exit"] == code
    assert data["answer"]["go"] is go
    assert data["answer"]["why"] == out["why"]
    assert data["answer"]["reading"] == out["reading"]
    assert data["limits"] == config.LIMITS == {"five_hour": 75, "seven_day": 85}
    text = render("budget", data, tmp_path)["text"]
    assert ("GO" if go else "WAIT") in text
    assert out["why"] in text
    assert "next refresh possible at" in text
    if not go:
        assert "wake at" in text


def test_budget_readings(running: Running, tmp_path: Path) -> None:
    data = running.get("/api/budget").json()
    shown = [r["session"] for r in data["readings"]]
    assert shown == [
        SESSIONS["fresh"][:8],
        SESSIONS["onewin"][:8],
        SESSIONS["stale"][:8],
    ]
    assert SESSIONS["old"][:8] not in shown
    assert TMP_SESSION[:8] not in shown
    assert data["session"] == NEWEST_SESSION[:8]
    onewin = data["readings"][1]
    assert onewin["windows"]["five_hour"] is None
    assert onewin["windows"]["seven_day"]["used_percentage"] == 61
    assert data["readings"][2]["stale"] is True
    text = render("budget", data, tmp_path)["text"]
    for s in shown:
        assert s in text
    for path in ("/api/budget", "/api/budget?refresh=1", "/api/jobs", "/api/health"):
        body = running.get(path).body
        for sid in list(SESSIONS.values()) + [TMP_SESSION]:
            assert sid.encode() not in body


def test_budget_without_reading_runs_without_session(factory: Factory) -> None:
    for f in (factory.root / "usage").iterdir():
        f.unlink()
    for running in start(factory):
        data = running.get("/api/budget").json()
    assert data["session"] is None
    (call,) = factory.calls()
    assert call["session"] is None


def test_doctor_answers_and_problems_first(factory: Factory, tmp_path: Path) -> None:
    for running in start(factory):
        ok = running.get("/api/health").json()
    assert ok["command"]["status"] == "answer"
    assert ok["problems"] == ["uv is not on PATH (venv builds)"]
    assert ok["doctor"]["verify"]["leftovers"]["worktrees"] == ["verify-wt-1"]
    factory.set_control("doctor", out=DOCTOR_LINEAR_DOWN, exit=1)
    for running in start(factory):
        down = running.get("/api/health").json()
    assert down["command"]["status"] == "answer"
    assert down["command"]["exit"] == 1
    assert down["problems"][0] == "uv is not on PATH (venv builds)"
    assert "Linear: HTTP 503 Service Unavailable" in " ".join(down["problems"])
    assert "key: missing" in down["problems"]
    out = render("health", down, tmp_path)
    text = out["text"]
    assert text.index("Problems") < text.index("Leftovers")
    assert "Linear: HTTP 503" in text
    assert "next refresh possible at" in text
    assert DOCTOR_OK["host"] in text


def test_doctor_helper_bug_and_garbage(factory: Factory, tmp_path: Path) -> None:
    factory.set_control("doctor", out={"x": 1}, exit=6)
    for running in start(factory):
        bug = running.get("/api/health").json()
    assert bug["command"]["status"] == "helper bug"
    assert "helper bug" in render("health", bug, tmp_path)["text"]


def test_short_session_id_is_never_shown_whole(factory: Factory) -> None:
    """An 8-character session name: at most half of it is shown, in the
    current-session field and in the readings list."""
    sid = "abcdefgh"
    (factory.root / "usage" / f"{sid}.json").write_text(
        json.dumps({"seen_at": int(time.time()), "rate_limits": {}})
    )
    for running in start(factory):
        res = running.get("/api/budget")
    data = res.json()
    assert factory.calls()[0]["session"] == sid
    assert data["session"] == "abcd"
    assert data["readings"][0]["session"] == "abcd"
    assert sid.encode() not in res.body


def test_helper_diagnostic_never_carries_the_session_or_a_path(
    factory: Factory,
) -> None:
    """The helper's `reading` can quote an OSError naming the usage file."""
    reading = (
        "unparsable: [Errno 2] No such file or directory: "
        f"'/srv/example/factory/usage/{NEWEST_SESSION}.json'"
    )
    factory.set_control(
        "budget",
        out={"go": True, "why": "usage data unreadable; starting", "reading": reading},
    )
    for running in start(factory):
        res = running.get("/api/budget")
    assert NEWEST_SESSION.encode() not in res.body
    assert b"/srv/example" not in res.body
    got = res.json()["answer"]["reading"]
    assert got == "unparsable: [Errno 2] No such file or directory: '<path>'"
    assert res.json()["answer"]["why"] == "usage data unreadable; starting"


def test_budget_stderr_is_scrubbed(factory: Factory) -> None:
    factory.set_control(
        "budget",
        out=BUDGET_GO,
        stderr=f"warning: cannot stat /srv/example/usage/{NEWEST_SESSION}.json\n",
    )
    for running in start(factory):
        res = running.get("/api/budget")
    assert NEWEST_SESSION.encode() not in res.body
    assert b"/srv/example" not in res.body
    assert res.json()["command"]["stderr"] == "warning: cannot stat <path>\n"


def test_wait_answer_shows_no_window_values_from_a_reading(
    running: Running, tmp_path: Path
) -> None:
    """WAIT carries no usage. The answer table stays empty, so a newer
    reading cannot contradict the cached answer; readings keep their values."""
    data = running.get("/api/budget").json()
    data["answer"] = dict(BUDGET_WAIT)
    html = render("budget", data, tmp_path)["html"]
    answer_table = html.split("<table")[1]
    assert "Default limit" in answer_table
    assert "<td>five_hour</td><td>-</td><td>-</td><td>75%</td>" in answer_table
    assert "40%" not in answer_table
    assert "40%" in html.split("Readings of the last 6 hours")[1]
