"""Acceptance criterion 3: the child gets a scratch environment."""

from __future__ import annotations

import pytest
from conftest import ALL_ROUTES, NEWEST_SESSION, Factory, start
from factory_ui import runner

SECRET = "s3cr3t-value"
BASE_NAMES = {
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "PYTHONDONTWRITEBYTECODE",
}


def test_child_env_has_only_allowed_names(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORY_BUDGET_LIMITS", "five_hour=1")
    monkeypatch.setenv("FACTORY_LINEAR_KEY_FILE", SECRET)
    monkeypatch.setenv("FAKE_SECRET_ENV", SECRET)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "server-own-session")
    monkeypatch.setenv("GIT_DIR", SECRET)
    bodies = []
    for running in start(factory):
        for path in ALL_ROUTES:
            bodies.append(running.get(path).body)
    calls = {c["argv"][1]: c for c in factory.calls()}
    assert set(calls) == {"doctor", "budget"}
    # The macOS loader adds __CF_USER_TEXT_ENCODING to every process.
    platform_added = {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}
    doctor_env = set(calls["doctor"]["env"]) - platform_added
    budget_env = set(calls["budget"]["env"]) - platform_added
    assert doctor_env <= BASE_NAMES
    assert "PYTHONDONTWRITEBYTECODE" in doctor_env
    assert budget_env <= BASE_NAMES | {"CLAUDE_CODE_SESSION_ID"}
    assert calls["budget"]["session"] == NEWEST_SESSION
    assert calls["doctor"]["session"] is None
    for c in calls.values():
        assert c["stdin_tty"] is False
    for body in bodies:
        assert SECRET.encode() not in body


def test_child_env_builder_never_passes_other_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "x")
    monkeypatch.setenv("FACTORY_HOST", "y")
    monkeypatch.setenv("HOME", "/tmp/fixture-home")
    env = runner.child_env("doctor", session_id=None)
    assert set(env) <= BASE_NAMES
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    env = runner.child_env("budget", session_id="abc_DEF-1")
    assert env["CLAUDE_CODE_SESSION_ID"] == "abc_DEF-1"
    with pytest.raises(ValueError):
        runner.child_env("budget", session_id="../x")
    with pytest.raises(ValueError):
        runner.child_env("doctor", session_id="abc")
