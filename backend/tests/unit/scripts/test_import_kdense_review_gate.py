"""import_kdense.py must not approve its own uploads by default (TS-36, GOO-630).

It used to create and approve every change request in one run with
self_approval_acknowledged and warning_acknowledged hard-coded to True, and it
took the bearer token on argv.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, List, Tuple

import pytest

pytestmark = pytest.mark.unit

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "project_skills"
    / "import_kdense.py"
)


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("import_kdense_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kdense = _load()


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> "FakeResponse":
        return self

    def json(self) -> dict:
        return self.payload


class FakeClient:
    posts: List[Tuple[str, dict]] = []

    def __init__(self, **_kwargs: Any) -> None:
        FakeClient.posts = []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, _url: str) -> FakeResponse:
        return FakeResponse({"skills": [], "pending_change_requests": []})

    def post(self, url: str, json: dict) -> FakeResponse:
        FakeClient.posts.append((url, json))
        return FakeResponse({"id": "cr-1"})


@pytest.fixture(autouse=True)
def fake_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("httpx")
    module.Client = FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", module)


def _approvals() -> List[dict]:
    return [body for url, body in FakeClient.posts if url.endswith("/approve")]


def test_upload_creates_pending_requests_without_approving() -> None:
    kdense.upload({"skill-a": "doc"}, "http://api", "p1", "tok")
    assert len(FakeClient.posts) == 1
    assert _approvals() == []


def test_self_approval_is_explicit_and_does_not_ack_warnings_by_default() -> None:
    kdense.upload({"skill-a": "doc"}, "http://api", "p1", "tok", self_approve=True)
    approvals = _approvals()
    assert len(approvals) == 1
    assert approvals[0]["warning_acknowledged"] is False


def test_token_is_not_accepted_on_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys, "argv", ["import_kdense.py", "--upload", "--token", "secret"]
    )
    monkeypatch.setattr(kdense, "scan_all", lambda: {})
    with pytest.raises(SystemExit) as excinfo:
        kdense.main()
    assert excinfo.value.code != 0
