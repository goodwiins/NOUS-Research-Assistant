"""backup-and-restore.sh restore and Velero behaviour (GOO-676).

Restore synced `backups/<id>`, a prefix no upload ever uses (uploads live at
`backups/postgres/postgres_backup_<id>.sql.gz` etc.), so it downloaded
nothing, restored nothing and still exited 0. Velero backups also included
Secrets while the help text said Secrets are never backed up.

The script runs with a minimal environment and logging stubs for kubectl,
aws, helm and velero; no cluster or bucket is touched.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "backup-and-restore.sh"
BACKUP_ID = "20261121_120000"

LOGGING_STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB_LOG"
cat > /dev/null 2>&1 < /dev/null
exit 0
"""

# aws stub: `s3 sync SRC DEST ...` materialises the postgres dump when
# STUB_HAS_BACKUP=1, mimicking a matching key in the bucket.
AWS_STUB = """#!/bin/sh
echo "aws $*" >> "$STUB_LOG"
if [ "$1" = "s3" ] && [ "$2" = "sync" ] && [ "${STUB_HAS_BACKUP:-0}" = "1" ]; then
    mkdir -p "$4/postgres"
    printf 'SELECT 1;\\n' | gzip > "$4/postgres/postgres_backup_${STUB_BACKUP_ID}.sql.gz"
fi
exit 0
"""


def _bash() -> str:
    bash = shutil.which("bash")
    assert bash, "bash is required"
    return bash


def _run(
    tmp_path: Path, args: list[str], extra_env: Optional[Dict[str, str]] = None
) -> Tuple[subprocess.CompletedProcess, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (
        ("kubectl", LOGGING_STUB),
        ("helm", LOGGING_STUB),
        ("velero", LOGGING_STUB),
        ("aws", AWS_STUB),
    ):
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "stub.log"
    log.touch()
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "STUB_LOG": str(log),
        "STUB_BACKUP_ID": BACKUP_ID,
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


def test_restore_fails_when_no_backup_matches(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, ["restore", BACKUP_ID])
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "Restore process initiated" not in output
    assert "kubectl exec" not in calls


def test_restore_downloads_the_component_keys_for_the_id(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, ["restore", BACKUP_ID], {"STUB_HAS_BACKUP": "1"})
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    sync = next(line for line in calls.splitlines() if line.startswith("aws s3 sync"))
    assert f"*_{BACKUP_ID}.*" in sync
    assert "kubectl exec -i" in calls and "psql" in calls


def test_restore_rejects_a_malformed_backup_id(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, ["restore", "../../etc"])
    assert result.returncode != 0
    assert "aws s3 sync" not in calls


def test_velero_backup_excludes_secrets() -> None:
    code = SCRIPT.read_text(encoding="utf-8")
    start = code.index("velero create backup")
    command = code[start : code.index("--wait", start)]
    assert "--exclude-resources secrets" in command
