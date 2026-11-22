"""Guards for deployment/deploy.sh (TD2B-1, TD2B-2; GOO-659).

The script did not parse (unquoted `-l app in (backend, frontend)`), and once
it did, `--rollback` always took the branch that scaled backend and frontend
to zero replicas and printed "Rollback completed successfully". These tests
run the rollback against a stub kubectl on PATH; no cluster is touched.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "deployment" / "deploy.sh"

STUB = """#!/usr/bin/env bash
echo "$*" >> "$KUBECTL_LOG"
if [[ "$1 $2" == "rollout history" ]]; then
    printf '%b' "$KUBECTL_HISTORY"
fi
exit 0
"""


def _bash() -> str:
    bash = shutil.which("bash")
    assert bash, "bash is required"
    return bash


def _run_rollback(
    tmp_path: Path, history: str
) -> tuple[subprocess.CompletedProcess, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(STUB)
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "kubectl.log"
    log.touch()
    env = dict(
        os.environ,
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        KUBECTL_LOG=str(log),
        KUBECTL_HISTORY=history,
        NAMESPACE="test-ns",
    )
    result = subprocess.run(
        [_bash(), str(SCRIPT), "--rollback"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return result, log.read_text()


def test_script_parses() -> None:
    result = subprocess.run(
        [_bash(), "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_rollback_without_previous_revision_fails_and_never_scales_to_zero(
    tmp_path: Path,
) -> None:
    result, calls = _run_rollback(
        tmp_path, "REVISION  CHANGE-CAUSE\\n1         <none>\\n"
    )
    assert result.returncode != 0
    assert "scale" not in calls
    assert "rollout undo" not in calls
    assert "completed" not in result.stdout.lower()


def test_rollback_undoes_both_deployments_when_history_exists(tmp_path: Path) -> None:
    result, calls = _run_rollback(
        tmp_path, "REVISION  CHANGE-CAUSE\\n1         <none>\\n2         <none>\\n"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "rollout undo deployment/backend -n test-ns" in calls
    assert "rollout undo deployment/frontend -n test-ns" in calls
    assert "scale" not in calls
