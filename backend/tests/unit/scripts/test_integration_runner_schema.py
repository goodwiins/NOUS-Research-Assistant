"""tests/integration/run_tests.py builds its schema with Alembic (GOO-681).

The runner used to apply database/migrations/*.sql file by file. None of
those files applies to a clean Postgres: they need Alembic-owned tables, and
several contain invalid DDL. So setup always failed. The runner now runs
`alembic upgrade head`, the real schema source, against its test database,
and stops mounting the legacy SQL.

The last test starts a throwaway, socket-only Postgres cluster and runs the
real upgrade through the runner. It needs the initdb/pg_ctl/psql binaries and
is skipped without them.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List

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


def _runner() -> Any:
    return runner_mod.IntegrationTestRunner(argparse.Namespace(log_level="error"))


class Recorder:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, self.returncode, "", "boom")


def test_schema_comes_from_alembic_upgrade_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = Recorder()
    monkeypatch.setattr(runner_mod.subprocess, "run", rec)
    assert _runner().apply_schema() is True
    call = rec.calls[0]
    assert call["cmd"][-3:] == ["alembic", "upgrade", "head"]
    assert Path(call["cwd"]) == REPO_ROOT / "backend"
    assert call["env"]["DATABASE_URL"] == runner_mod.TEST_DATABASE_URL
    assert "localhost:5433/test_analytics" in runner_mod.TEST_DATABASE_URL


def test_supabase_url_cannot_redirect_the_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # alembic/env.py prefers SUPABASE_DB_URL over DATABASE_URL.
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://real/db")
    rec = Recorder()
    monkeypatch.setattr(runner_mod.subprocess, "run", rec)
    _runner().apply_schema()
    assert "SUPABASE_DB_URL" not in rec.calls[0]["env"]


def test_failed_upgrade_fails_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod.subprocess, "run", Recorder(returncode=1))
    assert _runner().apply_schema() is False


def test_setup_builds_the_schema_after_services_are_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    order: List[str] = []

    class FakeDocker:
        def ping(self) -> None:
            pass

    async def step(name: str) -> bool:
        order.append(name)
        return True

    monkeypatch.setattr(runner_mod.docker, "from_env", lambda: FakeDocker())
    monkeypatch.setattr(runner, "setup_docker_containers", lambda: step("containers"))
    monkeypatch.setattr(runner, "wait_for_services", lambda: step("wait"))
    monkeypatch.setattr(runner, "install_dependencies", lambda: step("deps"))

    def failing_schema() -> bool:
        order.append("schema")
        return False

    monkeypatch.setattr(runner, "apply_schema", failing_schema)
    assert asyncio.run(runner.setup_environment()) is False
    assert order == ["containers", "wait", "schema"]


def test_legacy_sql_is_no_longer_mounted_or_applied() -> None:
    assert not hasattr(runner_mod, "apply_migrations")
    assert not hasattr(runner_mod.IntegrationTestRunner, "apply_migrations")
    assert "database/migrations" not in RUNNER.read_text(encoding="utf-8")
    assert '"migrations"' not in RUNNER.read_text(encoding="utf-8")


requires_pg = pytest.mark.skipif(
    not all(shutil.which(b) for b in ("initdb", "pg_ctl", "psql")),
    reason="needs local PostgreSQL server binaries",
)
PORT = "54332"


@pytest.fixture
def pg_url(tmp_path: Path) -> Iterator[str]:
    data = tmp_path / "data"
    # Unix socket paths are capped at ~104 bytes; tmp_path is too long.
    sock = Path(tempfile.mkdtemp(prefix="pg", dir="/tmp"))
    subprocess.run(
        ["initdb", "-D", str(data), "-U", "test_user", "--auth=trust"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "pg_ctl",
            "-s",
            "-D",
            str(data),
            "-l",
            str(tmp_path / "log"),
            "-o",
            f"-k {sock} -c listen_addresses=localhost -p {PORT}",
            "start",
            "-w",
        ],
        check=True,
        capture_output=True,
    )
    try:
        subprocess.run(
            ["createdb", "-h", str(sock), "-p", PORT, "-U", "test_user", "it"],
            check=True,
            capture_output=True,
        )
        yield f"postgresql://test_user@localhost:{PORT}/it"
    finally:
        subprocess.run(
            ["pg_ctl", "-s", "-D", str(data), "stop", "-m", "fast"],
            capture_output=True,
        )
        shutil.rmtree(sock, ignore_errors=True)


@pytest.mark.integration
@requires_pg
def test_runner_builds_the_schema_on_a_clean_postgres(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    assert _runner().apply_schema(database_url=pg_url) is True
    head = subprocess.run(
        ["psql", pg_url, "-tAc", "SELECT count(*) FROM alembic_version"],
        capture_output=True,
        text=True,
    )
    assert head.stdout.strip() == "1", head.stderr
