"""Acceptance criterion 2: the closed F allowlist."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conftest import ALL_ROUTES, PKG_ROOT, Factory, Running
from factory_ui import runner

FORBIDDEN = [
    "list",
    "stats",
    "gc",
    "show",
    "set",
    "verify-mode",
    "parallel",
    "slot",
    "claim",
]

HOSTILE = [
    "/api/health?cmd=list",
    "/api/health?refresh=1;list",
    "/api/health?refresh=1&&stats",
    "/api/budget?refresh=1&cmd=gc",
    "/api/health/list",
    "/api/health;list",
    "/api/health&&list",
    "/api/health$(list)",
    "/api/health%24%28list%29",
    "/api/budget?--apply",
    "/api/budget/--apply",
    "/api/gc?--apply",
    "/api/list",
    "/api/stats",
    "/api/show",
    "/api/jobs/2026-01-01-example-active?cmd=show",
    "/api/jobs/%3Blist",
    "/api/jobs/%26%26list",
    "/api/jobs/%2524%2528list%2529",
    "/api/verifier?mode=gate",
    "/api/parallel?cmd=slot",
    "/api/doctor",
    "/api/verify-mode",
    "/api/claim",
    "/%2e%2e/bin/factory",
    "/static/..%2fbin%2ffactory",
]


def test_allowed_has_exactly_doctor_and_budget() -> None:
    assert set(runner.ALLOWED) == {"doctor", "budget"}
    for name, argv in runner.ALLOWED.items():
        assert isinstance(argv, tuple)
        assert len(argv) == 2
        assert argv[0].endswith("/bin/factory")
        assert argv[1] == name


@pytest.mark.parametrize("name", FORBIDDEN)
def test_run_refuses_other_commands(name: str, factory: Factory) -> None:
    with pytest.raises(ValueError):
        runner.run(name, root=factory.root)
    assert factory.calls() == []


def test_routes_and_hostile_urls_spawn_only_the_two_argvs(running: Running) -> None:
    for path in ALL_ROUTES + HOSTILE:
        running.get(path)
    for path in ("/api/health?refresh=1", "/api/budget?refresh=1"):
        running.get(path)
    argvs = {tuple(c["argv"]) for c in running.factory.calls()}
    fbin = str(running.factory.root / "bin" / "factory")
    assert argvs == {(fbin, "doctor"), (fbin, "budget")}


def _modules() -> list[Path]:
    return sorted((PKG_ROOT / "factory_ui").rglob("*.py"))


def test_subprocess_only_in_runner_and_no_shell() -> None:
    importers = set()
    for path in _modules():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                names = []
            if any(n.split(".")[0] == "subprocess" for n in names):
                importers.add(path.name)
            if isinstance(node, ast.keyword) and node.arg == "shell":
                assert not (
                    isinstance(node.value, ast.Constant) and node.value.value is True
                ), f"shell=True in {path}"
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "os":
                    assert node.attr not in ("system", "popen"), f"os.{node.attr}"
                    assert not node.attr.startswith("exec"), f"os.{node.attr}"
                    assert not node.attr.startswith("spawn"), f"os.{node.attr}"
    assert importers == {"runner.py"}
