"""run_local_ci.sh's Trigger.dev gate trigger, run for real (GOO-782).

The parity tests check the gate lines statically. These run the script's own
prelude (``changed_paths`` and the reporting helpers) and its Trigger.dev
block in a throwaway git repository with logging ``pnpm``/``node`` fakes, so
the path list in ``TRIGGER_CHANGES`` and the missing-``node_modules`` failure
are pinned by behaviour, not by text.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
LOCAL_CI_PATH = REPO_ROOT / "scripts" / "ci" / "run_local_ci.sh"

# Every path whose change must arm the Trigger.dev gates, with a file under it.
# Removing a path from the script's list must fail its case below.
TRIGGER_PATHS = {
    "src/trigger": "src/trigger/bulk-ingest.ts",
    "tests/trigger": "tests/trigger/bulk-ingest.test.mjs",
    "backend/openapi.json": "backend/openapi.json",
    "tsconfig.json": "tsconfig.json",
    "trigger.config.ts": "trigger.config.ts",
    "package.json": "package.json",
    "pnpm-lock.yaml": "pnpm-lock.yaml",
}
TSC_CALL = "frontend|exec tsc --noEmit -p ../tsconfig.json"
NODE_TESTS = "--test tests/trigger/"
NO_NODE_MODULES = "node_modules are missing; run pnpm install --frozen-lockfile"

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _script() -> str:
    return LOCAL_CI_PATH.read_text(encoding="utf-8")


def trigger_harness(script: str) -> str:
    """The script's prelude plus its Trigger.dev block, made runnable alone."""
    prelude = script[
        script.index("FAILED=()\n") : script.index('step "Ruff (full tree, blocking)"')
    ]
    block = script[
        script.index("TRIGGER_CHANGES=") : script.index(
            'if [ "$DO_FRONTEND" -eq 1 ]; then\n  step "Frontend type-check'
        )
    ]
    return (
        "set -uo pipefail\n"
        'DO_FRONTEND="${DO_FRONTEND:-0}"; SKIP_TESTS="${SKIP_TESTS:-0}"\n'
        + prelude
        + 'MERGE_BASE="${CI_MERGE_BASE:-}"\n'
        + block
        + 'printf "FAILED=%s\\n" "${FAILED[*]:-}"\n'
        + 'printf "SKIPPED=%s\\n" "${SKIPPED[*]:-}"\n'
        + '[ "${#FAILED[@]}" -eq 0 ]\n'
    )


def script_trigger_paths(script: str) -> list[str]:
    match = re.search(r'TRIGGER_CHANGES="\$\(changed_paths (.*?)\)"', script, re.S)
    assert match, "TRIGGER_CHANGES is no longer a changed_paths call"
    return shlex.split(match.group(1).replace("\\\n", " "))


@dataclass
class Run:
    returncode: int
    stdout: str
    pnpm: list[str]
    node: list[str]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git checkout with one base commit and the frontend/ dir tsc runs from."""
    root = tmp_path / "checkout"
    (root / "frontend").mkdir(parents=True)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "ci@example.invalid")
    _git(root, "config", "user.name", "ci")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _install_tsc(repo: Path, where: str = "node_modules/.bin") -> None:
    tsc = repo / where / "tsc"
    tsc.parent.mkdir(parents=True, exist_ok=True)
    tsc.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    tsc.chmod(0o755)


def _change(repo: Path, relpath: str, *, commit: bool = True) -> None:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed\n", encoding="utf-8")
    if commit:
        _git(repo, "add", relpath)
        _git(repo, "commit", "-q", "-m", f"change {relpath}")


def run_trigger_block(
    repo: Path,
    tmp_path: Path,
    *,
    frontend: bool = False,
    skip_tests: bool = False,
    merge_base: str | None = None,
    script: str | None = None,
) -> Run:
    fakes = tmp_path / "fakes"
    fakes.mkdir(exist_ok=True)
    for tool in ("pnpm", "node"):
        fake = fakes / tool
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s|%s\\n" "${{PWD##*/}}" "$*" >> "$CI_LOG_DIR/{tool}.log"\n',
            encoding="utf-8",
        )
        fake.chmod(0o755)
    harness = tmp_path / "harness.sh"
    harness.write_text(trigger_harness(script or _script()), encoding="utf-8")
    if merge_base is None:
        merge_base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    result = subprocess.run(
        ["bash", str(harness)],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}",
            "CI_LOG_DIR": str(tmp_path),
            "CI_MERGE_BASE": merge_base,
            "DO_FRONTEND": "1" if frontend else "0",
            "SKIP_TESTS": "1" if skip_tests else "0",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    def log(tool: str) -> list[str]:
        path = tmp_path / f"{tool}.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    return Run(
        result.returncode, result.stdout + result.stderr, log("pnpm"), log("node")
    )


def _fired(run: Run) -> bool:
    return TSC_CALL in run.pnpm


def test_the_trigger_path_list_is_pinned() -> None:
    assert sorted(script_trigger_paths(_script())) == sorted(TRIGGER_PATHS)


@needs_git
@pytest.mark.parametrize("pathspec", sorted(TRIGGER_PATHS))
def test_a_change_under_each_listed_path_fires_the_trigger_gates(
    repo: Path, tmp_path: Path, pathspec: str
) -> None:
    _install_tsc(repo)
    _change(repo, TRIGGER_PATHS[pathspec])

    run = run_trigger_block(repo, tmp_path)

    assert run.returncode == 0, run.stdout
    assert _fired(run), run.stdout
    # The node tests run from the checkout root, as the CI step does.
    assert any(
        call.startswith("checkout|") and NODE_TESTS in call for call in run.node
    ), run.node
    assert "Trigger.dev tsc" in run.stdout


@needs_git
def test_an_untracked_trigger_file_fires_the_gates(repo: Path, tmp_path: Path) -> None:
    _install_tsc(repo)
    _change(repo, "src/trigger/new-task.ts", commit=False)

    run = run_trigger_block(repo, tmp_path)

    assert run.returncode == 0, run.stdout
    assert _fired(run), run.stdout


@needs_git
@pytest.mark.parametrize(
    "relpath",
    ["frontend/src/page.tsx", "docs/notes.md", "src/trigger-old/task.ts", "src/x.ts"],
)
def test_an_unrelated_change_skips_the_gates_visibly(
    repo: Path, tmp_path: Path, relpath: str
) -> None:
    _install_tsc(repo)
    _change(repo, relpath)

    run = run_trigger_block(repo, tmp_path)

    assert run.returncode == 0, run.stdout
    assert run.pnpm == [] and run.node == [], run.stdout
    assert "SKIPPED=Trigger.dev type-check + node tests" in run.stdout


@needs_git
def test_frontend_fires_the_gates_with_nothing_changed(
    repo: Path, tmp_path: Path
) -> None:
    _install_tsc(repo)

    run = run_trigger_block(repo, tmp_path, frontend=True)

    assert run.returncode == 0, run.stdout
    assert _fired(run), run.stdout


@needs_git
def test_an_unresolved_merge_base_fires_the_gates(repo: Path, tmp_path: Path) -> None:
    """changed_paths reports `<unresolved-base>`, so the gates run, not skip."""
    _install_tsc(repo)

    run = run_trigger_block(repo, tmp_path, merge_base="")

    assert _fired(run), run.stdout


@needs_git
def test_skip_tests_runs_tsc_and_reports_the_node_tests_skipped(
    repo: Path, tmp_path: Path
) -> None:
    _install_tsc(repo)
    _change(repo, "src/trigger/bulk-ingest.ts")

    run = run_trigger_block(repo, tmp_path, skip_tests=True)

    assert run.returncode == 0, run.stdout
    assert _fired(run), run.stdout
    assert run.node == [], run.stdout
    assert "SKIPPED=Trigger.dev node tests" in run.stdout


@needs_git
@pytest.mark.parametrize("frontend", [False, True])
def test_missing_node_modules_fails_with_a_clear_message(
    repo: Path, tmp_path: Path, frontend: bool
) -> None:
    _change(repo, "src/trigger/bulk-ingest.ts")

    run = run_trigger_block(repo, tmp_path, frontend=frontend)

    assert run.returncode != 0, run.stdout
    assert NO_NODE_MODULES in run.stdout
    assert "FAILED=Trigger.dev gates (no node_modules)" in run.stdout
    # Neither gate ran, so nothing can read as a pass.
    assert run.pnpm == [] and run.node == [], run.stdout


@needs_git
def test_a_non_executable_tsc_counts_as_missing(repo: Path, tmp_path: Path) -> None:
    _install_tsc(repo)
    (repo / "node_modules" / ".bin" / "tsc").chmod(0o644)
    _change(repo, "src/trigger/bulk-ingest.ts")

    run = run_trigger_block(repo, tmp_path)

    assert run.returncode != 0, run.stdout
    assert NO_NODE_MODULES in run.stdout


@needs_git
def test_a_frontend_local_tsc_is_enough(repo: Path, tmp_path: Path) -> None:
    _install_tsc(repo, "frontend/node_modules/.bin")
    _change(repo, "src/trigger/bulk-ingest.ts")

    run = run_trigger_block(repo, tmp_path)

    assert run.returncode == 0, run.stdout
    assert _fired(run), run.stdout
