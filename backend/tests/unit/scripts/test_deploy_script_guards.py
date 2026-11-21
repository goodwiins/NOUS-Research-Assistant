"""Guards for scripts/deploy.sh (TS-10, TS-11; part of TS-H, GOO-630).

deploy.sh continued after a failed database backup, ran `docker-compose down`
before `up` (a failed `up` left the environment stopped), had no production
guard, and its rollback ignored the backup-path argument and printed
"Rollback completed successfully" without restoring the database.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "deploy.sh"


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


def test_script_parses() -> None:
    result = subprocess.run(
        [_bash(), "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("command", ["deploy", "deploy-blue-green", "rollback"])
def test_production_requires_explicit_confirmation(command: str) -> None:
    env = {k: v for k, v in os.environ.items() if k != "CONFIRM_PRODUCTION"}
    env["ENVIRONMENT"] = "production"
    # The guard runs before any dependency check or side effect.
    result = subprocess.run(
        [_bash(), str(SCRIPT), command],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode != 0
    assert "CONFIRM_PRODUCTION=yes" in result.stdout + result.stderr


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
