"""Guards for scripts/deploy.sh (TS-10, TS-11; part of TS-H, GOO-630).

deploy.sh continued after a failed database backup, ran `docker-compose down`
before `up` (a failed `up` left the environment stopped), had no production
guard, and its rollback ignored the backup-path argument and printed
"Rollback completed successfully" without restoring the database.

Every test that executes the script runs it with a minimal environment and a
PATH whose docker, docker-compose, curl and jq are logging stubs, so nothing
real can be touched.
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "deploy.sh"

STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB_LOG"
exit 0
"""


def _code() -> str:
    return "\n".join(
        line
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _bash() -> str:
    bash = shutil.which("bash")
    assert bash, "bash is required"
    return bash


def _run(
    tmp_path: Path, args: list[str], extra_env: Optional[Dict[str, str]] = None
) -> Tuple[subprocess.CompletedProcess, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("docker", "docker-compose", "curl", "jq"):
        stub = bin_dir / name
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "stub.log"
    log.touch()
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "STUB_LOG": str(log),
        "MAX_RETRIES": "1",
        "RETRY_DELAY": "0",
    }
    env.update(extra_env or {})
    result = subprocess.run(
        [_bash(), str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    return result, log.read_text()


def test_script_parses() -> None:
    result = subprocess.run(
        [_bash(), "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("environment", ["prod", "production", "PROD", "Production"])
@pytest.mark.parametrize("command", ["deploy", "deploy-blue-green", "rollback"])
def test_every_prod_spelling_requires_explicit_confirmation(
    tmp_path: Path, environment: str, command: str
) -> None:
    result, calls = _run(tmp_path, [command], {"ENVIRONMENT": environment})
    assert result.returncode != 0
    assert "CONFIRM_PRODUCTION=yes" in result.stdout + result.stderr
    assert calls == "", "nothing may run before the production gate"


def test_unknown_environment_is_refused(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, ["deploy"], {"ENVIRONMENT": "prd"})
    assert result.returncode != 0
    assert "ENVIRONMENT" in result.stdout + result.stderr
    assert calls == ""


def test_prod_spellings_resolve_to_the_real_compose_file() -> None:
    assert (REPO_ROOT / "docker-compose.prod.yml").is_file()
    code = _code()
    assert re.search(r"prod\|production\)", code)


def test_compose_never_removes_orphans() -> None:
    # Blue-green containers live in the same compose project; --remove-orphans
    # would delete the live backend-blue/backend-green.
    assert "--remove-orphans" not in _code()


def test_rollback_is_reported_as_partial_and_exits_non_zero(tmp_path: Path) -> None:
    backup = tmp_path / "pre-deploy-20261121_000000"
    backup.mkdir()
    result, calls = _run(
        tmp_path,
        ["rollback", str(backup)],
        {
            "ENVIRONMENT": "staging",
            "SECRET_KEY": "x",
            "DATABASE_URL": "postgresql://stub/db",
            "REDIS_URL": "redis://stub",
        },
    )
    output = result.stdout + result.stderr
    assert result.returncode == 3, output
    assert "PARTIAL ROLLBACK" in output
    assert "Operation completed successfully" not in output
    assert "docker-compose" in calls and "--remove-orphans" not in calls


def test_failed_database_backup_aborts_the_deploy() -> None:
    line = next(line for line in _code().splitlines() if "backup_database.sh" in line)
    assert "error_exit" in line and "warning" not in line


def test_deploy_never_tears_down_before_starting() -> None:
    assert not re.search(r"docker-compose -f \"\$COMPOSE_FILE\" down", _code())


def test_rollback_honours_backup_path_and_does_not_claim_db_restore() -> None:
    code = _code()
    assert 'rollback "${2:-}"\n' in code, "rollback must receive the backup path"
    assert "Rollback completed successfully" not in code
    assert "DATABASE WAS NOT RESTORED" in code


def test_missing_vector_store_backup_step_is_gone() -> None:
    assert "backup_vector_store.py" not in _code()
