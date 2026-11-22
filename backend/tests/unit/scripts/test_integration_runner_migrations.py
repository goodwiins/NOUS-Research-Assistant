"""tests/integration/run_tests.py must apply legacy SQL fail-fast (GOO-674).

The runner mounted all of database/migrations/ into the Postgres image's
init directory. A failing script made the container exit (and, with
remove=True, vanish), so the runner only reported a health-check timeout;
and the rollback script ran right after the schema it undoes. Now each
file is applied explicitly with `psql -v ON_ERROR_STOP=1`, rollback files
are skipped, and setup fails on the first non-zero exit.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from typing import Any, List

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER = REPO_ROOT / "tests" / "integration" / "run_tests.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "integration_runner_under_test", RUNNER
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner_mod = _load()


class ExecResult:
    def __init__(self, exit_code: int, output: bytes = b"") -> None:
        self.exit_code = exit_code
        self.output = output


class FakeContainer:
    def __init__(self, fail_on: str = "") -> None:
        self.fail_on = fail_on
        self.commands: List[List[str]] = []

    def exec_run(self, cmd: List[str], **_kwargs: Any) -> ExecResult:
        self.commands.append(cmd)
        if self.fail_on and any(self.fail_on in part for part in cmd):
            return ExecResult(3, b'ERROR:  relation "x" does not exist')
        return ExecResult(0)


def _runner() -> Any:
    args = argparse.Namespace(log_level="error")
    return runner_mod.IntegrationTestRunner(args)


def _write(dir_: Path, *names: str) -> None:
    for name in names:
        (dir_ / name).write_text("SELECT 1;\n")


def test_migration_files_are_sorted_and_skip_rollbacks(tmp_path: Path) -> None:
    _write(tmp_path, "002_b.sql", "001_rollback_a.sql", "001_a.sql", "notes.md")
    names = [p.name for p in runner_mod.migration_files(tmp_path)]
    assert names == ["001_a.sql", "002_b.sql"]


def test_every_file_is_applied_with_on_error_stop(tmp_path: Path) -> None:
    _write(tmp_path, "001_a.sql", "002_b.sql")
    container = FakeContainer()
    runner = _runner()
    runner.containers["postgres"] = container

    assert runner.apply_migrations(tmp_path) is True
    assert len(container.commands) == 2
    for cmd in container.commands:
        assert cmd[0] == "psql"
        assert "ON_ERROR_STOP=1" in cmd


def test_failing_file_stops_setup(tmp_path: Path) -> None:
    _write(tmp_path, "001_a.sql", "002_broken.sql", "003_c.sql")
    container = FakeContainer(fail_on="002_broken.sql")
    runner = _runner()
    runner.containers["postgres"] = container

    assert runner.apply_migrations(tmp_path) is False
    assert not any("003_c.sql" in part for cmd in container.commands for part in cmd)


def test_migrations_are_not_mounted_as_init_scripts() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    assert "/docker-entrypoint-initdb.d" not in source
