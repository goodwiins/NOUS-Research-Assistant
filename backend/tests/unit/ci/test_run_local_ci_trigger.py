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
    "src": "src/x.ts",
    "tests/trigger": "tests/trigger/bulk-ingest.test.mjs",
    "backend/openapi.json": "backend/openapi.json",
    "frontend/app": "frontend/app/page.tsx",
    "frontend/src": "frontend/src/page.tsx",
    "terminal/src": "terminal/src/app.tsx",
    "packages": "packages/harness-bridge/src/index.ts",
    "frontend/package.json": "frontend/package.json",
    "tsconfig.json": "tsconfig.json",
    "trigger.config.ts": "trigger.config.ts",
    "package.json": "package.json",
    "pnpm-lock.yaml": "pnpm-lock.yaml",
}
TSC_CALL = "frontend|exec tsc --noEmit -p ../tsconfig.json"
NODE_TESTS = "--test tests/trigger/"
NO_NODE_MODULES = "node_modules are missing; run pnpm install --frozen-lockfile"
NO_TEST_FILES = "Trigger.dev test files (tests/trigger/*.test.mjs)"
ENUMERATOR_FAILED = "list changed Trigger.dev paths"
CONTRACT_TEST = REPO_ROOT / "tests" / "trigger" / "backend-paths-contract.test.mjs"

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
    """A git checkout with one base commit, the frontend/ dir tsc runs from,
    and a Trigger.dev test file for the node tests' glob to match."""
    root = tmp_path / "checkout"
    (root / "frontend").mkdir(parents=True)
    (root / "tests" / "trigger").mkdir(parents=True)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    (root / "tests" / "trigger" / "base.test.mjs").write_text("\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "ci@example.invalid")
    _git(root, "config", "user.name", "ci")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "README.md", "tests/trigger/base.test.mjs")
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


def _covers(pathspec: str, path: str) -> bool:
    return path == pathspec or path.startswith(pathspec.rstrip("/") + "/")


def test_every_task_caller_dir_the_contract_scans_arms_the_gates() -> None:
    """The path contract reads these trees, so a change there must fire it."""
    source = CONTRACT_TEST.read_text(encoding="utf-8")
    match = re.search(r"const TASK_CALLER_DIRS = \[(.*?)\];", source, re.S)
    assert match, "TASK_CALLER_DIRS is no longer a literal array"
    dirs = re.findall(r'"([^"]+)"', match.group(1))
    assert dirs
    paths = script_trigger_paths(_script())
    uncovered = [d for d in dirs if not any(_covers(p, d) for p in paths)]
    assert uncovered == []


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
    ["docs/notes.md", "backend/src/x.py", "frontend/public/x.svg"],
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


@needs_git
def test_no_trigger_test_file_fails_the_node_tests(repo: Path, tmp_path: Path) -> None:
    """node --test passes with zero tests on an unmatched glob; the guard fails."""
    _install_tsc(repo)
    _git(repo, "rm", "-q", "tests/trigger/base.test.mjs")
    _git(repo, "commit", "-q", "-m", "drop the trigger tests")
    _change(repo, "src/trigger/bulk-ingest.ts")

    run = run_trigger_block(repo, tmp_path)

    assert run.returncode != 0, run.stdout
    assert f"FAILED={NO_TEST_FILES}" in run.stdout


@needs_git
def test_skip_tests_does_not_run_the_test_file_guard(
    repo: Path, tmp_path: Path
) -> None:
    _install_tsc(repo)
    _git(repo, "rm", "-q", "tests/trigger/base.test.mjs")
    _git(repo, "commit", "-q", "-m", "drop the trigger tests")
    (repo / "tests" / "trigger").mkdir(parents=True, exist_ok=True)
    _change(repo, "src/trigger/bulk-ingest.ts")

    run = run_trigger_block(repo, tmp_path, skip_tests=True)

    assert run.returncode == 0, run.stdout
    assert NO_TEST_FILES not in run.stdout
    assert "SKIPPED=Trigger.dev node tests" in run.stdout


@needs_git
def test_a_failed_enumerator_fails_the_run(repo: Path, tmp_path: Path) -> None:
    """A base git diff cannot read must not read as "no Trigger.dev change"."""
    _install_tsc(repo)

    run = run_trigger_block(repo, tmp_path, merge_base="0" * 40)

    assert run.returncode != 0, run.stdout
    failed = next(ln for ln in run.stdout.splitlines() if ln.startswith("FAILED="))
    assert ENUMERATOR_FAILED in failed, run.stdout


def _changed_paths(
    repo: Path, tmp_path: Path, merge_base: str
) -> subprocess.CompletedProcess[str]:
    script = _script()
    prelude = script[
        script.index("FAILED=()\n") : script.index('step "Ruff (full tree, blocking)"')
    ]
    harness = tmp_path / "changed_paths.sh"
    harness.write_text(
        "set -uo pipefail\n"
        + prelude
        + f"MERGE_BASE={shlex.quote(merge_base)}\n"
        + "changed_paths src\n",
        encoding="utf-8",
    )
    return subprocess.run(
        ["bash", str(harness)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )


@needs_git
def test_changed_paths_returns_git_diffs_failure(repo: Path, tmp_path: Path) -> None:
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    assert _changed_paths(repo, tmp_path, base).returncode == 0
    assert _changed_paths(repo, tmp_path, "0" * 40).returncode != 0


@needs_git
def test_changed_paths_prints_the_sorted_union(repo: Path, tmp_path: Path) -> None:
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    _change(repo, "src/b.ts")
    _change(repo, "src/c.ts")
    _change(repo, "src/c.ts", commit=False)
    _change(repo, "src/a.ts", commit=False)

    result = _changed_paths(repo, tmp_path, base)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "src/a.ts\nsrc/b.ts\nsrc/c.ts\n"
