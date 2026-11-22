"""001_add_monitoring_schema.sql must apply where rag_app_user does not exist (GOO-681).

The grant block granted to `rag_app_user`, which no script creates, so the
file failed under ON_ERROR_STOP on a stock Postgres. It also granted USAGE on
`<table>_id_seq`, but every monitoring table has a UUID id and so no sequence.

The test starts a throwaway, socket-only Postgres cluster in tmp_path. It
needs the initdb/pg_ctl/psql binaries and is skipped without them. The file
references `users` and `organizations`, which Alembic owns, so minimal
stand-ins are created first.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator, List

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATION = REPO_ROOT / "database" / "migrations" / "001_add_monitoring_schema.sql"
PORT = "54331"
PREREQS = """
CREATE TABLE organizations (id UUID PRIMARY KEY);
CREATE TABLE users (id UUID PRIMARY KEY);
"""

requires_pg = pytest.mark.skipif(
    not all(shutil.which(b) for b in ("initdb", "pg_ctl", "psql")),
    reason="needs local PostgreSQL server binaries",
)


@pytest.fixture
def pg(tmp_path: Path) -> Iterator[List[str]]:
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
            f"-k {sock} -c listen_addresses='' -p {PORT}",
            "start",
            "-w",
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield [
            "psql",
            "-h",
            str(sock),
            "-p",
            PORT,
            "-U",
            "test_user",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "--no-psqlrc",
            "-q",
        ]
    finally:
        subprocess.run(
            ["pg_ctl", "-s", "-D", str(data), "stop", "-m", "fast"],
            capture_output=True,
        )
        shutil.rmtree(sock, ignore_errors=True)


def _psql(base: List[str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*base, *args], capture_output=True, text=True, timeout=120)


@requires_pg
def test_applies_when_the_app_role_is_missing(pg: List[str]) -> None:
    assert _psql(pg, "-c", PREREQS).returncode == 0
    result = _psql(pg, "-f", str(MIGRATION))
    assert result.returncode == 0, result.stderr


@requires_pg
def test_grants_to_the_app_role_when_it_exists(pg: List[str]) -> None:
    assert _psql(pg, "-c", PREREQS + "CREATE ROLE rag_app_user;").returncode == 0
    result = _psql(pg, "-f", str(MIGRATION))
    assert result.returncode == 0, result.stderr
    check = _psql(
        pg,
        "-tAc",
        "SELECT has_table_privilege('rag_app_user', 'system_alerts', 'INSERT')",
    )
    assert check.stdout.strip() == "t", check.stderr
