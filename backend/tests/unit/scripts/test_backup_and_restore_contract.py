"""Static guards for scripts/backup-and-restore.sh (audit finding TS-02, GOO-629).

The script dumped every Kubernetes Secret in the namespace as plaintext YAML
into a world-readable /tmp directory and uploaded the tarball to S3 without
encryption. These tests pin the fix without running kubectl or aws.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "backup-and-restore.sh"


def _code_lines() -> list[str]:
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if not line.lstrip().startswith("#")]


def test_script_parses() -> None:
    bash = shutil.which("bash")
    assert bash, "bash is required"
    result = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_kubernetes_secrets_are_never_dumped() -> None:
    for line in _code_lines():
        if re.search(r"\bkubectl\s+get\b", line):
            assert not re.search(
                r"\bsecrets?\b", line
            ), f"kubectl get must not read Secrets: {line.strip()}"


def test_every_s3_upload_is_kms_encrypted() -> None:
    uploads = [line for line in _code_lines() if re.search(r"\baws\s+s3\s+cp\b", line)]
    assert uploads, "expected the upload helper to call aws s3 cp"
    for line in uploads:
        assert "--sse aws:kms" in line, f"unencrypted upload: {line.strip()}"


def test_local_backups_are_private() -> None:
    code = "\n".join(_code_lines())
    assert re.search(r"^umask 077$", code, re.M), "local backup files must be 0600/0700"
    assert not re.search(
        r'BACKUP_DIR="\$\{BACKUP_DIR:-/tmp/', code
    ), "do not default to a fixed, shared /tmp path"


def test_system_schema_filters_match_a_literal_underscore() -> None:
    # In LIKE a bare _ matches any character, so 'pg_temp%' would also drop a
    # user schema such as pgxtemp from the restore's emptiness count.
    patterns = re.findall(r"nspname NOT LIKE '([^']*)'", "\n".join(_code_lines()))
    assert patterns == [r"pg\_toast%", r"pg\_temp%"]
