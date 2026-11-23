"""`.specify/scripts/bash/common.sh` must not let a branch name run commands.

TS-17 (part of TS-H, GOO-630): get_feature_paths printed
`CURRENT_BRANCH='$current_branch'` and callers ran `eval $(get_feature_paths)`,
so a branch or SPECIFY_FEATURE value containing `'$(cmd)'` executed `cmd`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
COMMON = REPO_ROOT / ".specify" / "scripts" / "bash" / "common.sh"


def test_crafted_feature_name_is_not_executed(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    assert bash, "bash is required"
    marker = tmp_path / "pwned"
    feature = f"001-x'$(touch {marker})'"
    script = f'source "{COMMON}"\neval "$(get_feature_paths)"\nprintf "%s" "$CURRENT_BRANCH"\n'
    env = dict(os.environ, SPECIFY_FEATURE=feature)

    result = subprocess.run(
        [bash, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "branch name was executed as a command"
    assert result.stdout == feature
