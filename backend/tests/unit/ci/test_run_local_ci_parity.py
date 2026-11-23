"""run_local_ci.sh must run the gates CI runs, the way CI runs them (GOO-738).

Each contract below is a gap that let a branch pass locally and fail in CI:
backend tests run from ``backend/`` instead of the repo root, architecture
tests skipped with ``--skip-tests``, and no frontend coverage ratchet.
"""

from __future__ import annotations

import copy
import re
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]  # CI lint gate installs no types-PyYAML

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
LOCAL_CI_PATH = REPO_ROOT / "scripts" / "ci" / "run_local_ci.sh"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test-pipeline.yml"

SKIP_TESTS_OPEN = 'if [ "$SKIP_TESTS" -eq 0 ]; then\n'


def _script() -> str:
    return LOCAL_CI_PATH.read_text(encoding="utf-8")


def _block(script: str, opener: str) -> str:
    """Return the text of the top-level ``if`` block that starts at ``opener``."""
    start = script.index(opener)
    end = script.index("\nfi\n", start)
    return script[start:end]


def test_backend_tests_run_from_the_repo_root_like_ci() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    script = _script()

    # The unit-tests job: `pytest backend/tests/ -c backend/pytest.ini` from
    # the checkout root, never from backend/.
    assert "pytest backend/tests/ \\\n            -c backend/pytest.ini" in workflow
    assert '( cd backend && "$PY" -m pytest' not in script
    assert "backend_pytest backend/tests/" in script
    assert '-c backend/pytest.ini -m "$BACKEND_UNIT_MARKERS"' in script
    # `python -m` puts the cwd on sys.path; bare `pytest` (CI) does not.
    assert '"$PY" -P -m pytest "$@"' in script


# Workflow-level variables that configure the runner, not the tests.
RUNNER_ONLY_ENV = frozenset(
    {
        "PYTHON_VERSION",
        "NODE_VERSION",
        "DOCKER_BUILDKIT",
        "PIP_DISABLE_PIP_VERSION_CHECK",
    }
)
# CI-only reporting/parallelism flags; local runs stay quiet and uncovered.
CI_REPORTING_FLAGS = frozenset(
    {"--cov", "--cov-report", "--cov-fail-under", "--junitxml", "-n", "-v"}
)
# Flags whose value is the next token.
VALUE_FLAGS = frozenset({"-c", "-m", "-n", "-p", "-k", "-o", "--ignore", "--deselect"})


def _workflow() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return doc


def _unit_test_step(workflow: dict[str, Any]) -> dict[str, Any]:
    steps = workflow["jobs"]["unit-tests"]["steps"]
    matches = [s for s in steps if "pytest backend/tests/" in str(s.get("run", ""))]
    assert len(matches) == 1, "expected one unit-tests pytest step"
    step: dict[str, Any] = matches[0]
    return step


def ci_unit_env(workflow: dict[str, Any]) -> dict[str, str]:
    """Env the CI unit-test pytest sees: workflow, then job, then step."""
    env = {
        k: str(v)
        for k, v in (workflow.get("env") or {}).items()
        if k not in RUNNER_ONLY_ENV
    }
    env.update(
        {
            k: str(v)
            for k, v in (workflow["jobs"]["unit-tests"].get("env") or {}).items()
        }
    )
    env.update(
        {k: str(v) for k, v in (_unit_test_step(workflow).get("env") or {}).items()}
    )
    return env


def _helper(script: str) -> str:
    start = script.index("backend_pytest() {")
    return script[start : script.index("\n}\n", start)]


def local_helper_env(script: str) -> dict[str, str]:
    """`NAME=value \\` assignments that prefix the local pytest command."""
    env: dict[str, str] = {}
    for line in _helper(script).splitlines():
        m = re.match(r'^\s*([A-Z_][A-Z0-9_]*)=("?)(.*?)\2\s*\\$', line)
        if m:
            env[m.group(1)] = m.group(3)
    return env


def _parse_args(tokens: list[str]) -> tuple[list[str], set[tuple[str, str]]]:
    """Split pytest args into positionals and (flag, value) pairs."""
    positionals: list[str] = []
    flags: set[tuple[str, str]] = set()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-"):
            if "=" in tok:
                name, _, value = tok.partition("=")
                flags.add((name, value))
            elif tok in VALUE_FLAGS and i + 1 < len(tokens):
                flags.add((tok, tokens[i + 1]))
                i += 1
            else:
                flags.add((tok, ""))
        else:
            positionals.append(tok)
        i += 1
    return positionals, flags


def ci_pytest_args(workflow: dict[str, Any]) -> tuple[list[str], set[tuple[str, str]]]:
    run = str(_unit_test_step(workflow)["run"]).replace("\\\n", " ")
    line = next(ln for ln in run.splitlines() if ln.strip().startswith("pytest "))
    return _parse_args(shlex.split(line)[1:])


def local_pytest_args(script: str) -> tuple[list[str], set[tuple[str, str]]]:
    """Args of the local unit-suite call, with the helper's fixed args added."""
    helper = _helper(script).replace("\\\n", " ")
    markers = re.search(r'^BACKEND_UNIT_MARKERS="([^"]*)"', script, re.M)
    assert markers, "BACKEND_UNIT_MARKERS is not defined"
    fixed = helper[helper.index('pytest "$@"') + len('pytest "$@"') :]
    fixed = fixed.replace('"$BACKEND_UNIT_MARKERS"', shlex.quote(markers.group(1)))
    call = re.search(r"^\s*backend_pytest (backend/tests/ .*)$", script, re.M)
    assert call, "the local unit-suite call is missing"
    return _parse_args(shlex.split(call.group(1)) + shlex.split(fixed))


def env_drift(workflow: dict[str, Any], script: str) -> list[str]:
    ci, local = ci_unit_env(workflow), local_helper_env(script)
    return sorted(
        f"{k}: CI={ci.get(k)!r} local={local.get(k)!r}"
        for k in ci.keys() | local.keys()
        if ci.get(k) != local.get(k)
    )


def pytest_drift(workflow: dict[str, Any], script: str) -> list[str]:
    ci_pos, ci_flags = ci_pytest_args(workflow)
    local_pos, local_flags = local_pytest_args(script)
    drift = [f"test path: CI={ci_pos} local={local_pos}"] if ci_pos != local_pos else []
    for name, value in sorted(ci_flags):
        if name in CI_REPORTING_FLAGS:
            continue
        if (name, value) not in local_flags:
            drift.append(f"CI passes {name} {value!r}; the local run does not")
    return drift


def test_backend_tests_get_the_ci_unit_env() -> None:
    """Every variable CI's unit-test pytest sees, derived from the workflow."""
    assert env_drift(_workflow(), _script()) == []


def test_backend_pytest_args_match_the_workflow() -> None:
    """Every non-reporting pytest flag and the test path, derived from the workflow."""
    assert pytest_drift(_workflow(), _script()) == []


def test_trigger_steps_mirror_the_frontend_tests_job() -> None:
    steps = {s.get("name"): s for s in _workflow()["jobs"]["frontend-tests"]["steps"]}
    node_step = steps["Check Trigger.dev tasks (node:test)"]
    tsc_step = steps["Type-check Trigger.dev tasks (tsc)"]
    script = _script()
    # node:test runs from the repo root in both places.
    assert node_step["working-directory"] == "."
    assert f"      {node_step['run'].strip()}\n" in script
    # tsc runs from frontend/ (the job default) in both places.
    assert "working-directory" not in tsc_step
    assert f"( cd frontend && {tsc_step['run'].strip()} )" in script
    assert 'check $? "Trigger.dev tsc"' in script
    assert 'check $? "Trigger.dev node tests"' in script
    assert 'skipped "Trigger.dev node tests" "--skip-tests"' in script


def test_architecture_tests_run_even_with_skip_tests() -> None:
    script = _script()
    command = "backend_pytest backend/tests/unit/architecture"
    assert command in script
    assert 'check $? "backend architecture tests"' in script
    # Not gated behind --skip-tests: outside the block that --skip-tests skips.
    skip_block = _block(script, SKIP_TESTS_OPEN)
    assert command not in skip_block
    assert script.index(command) < script.index(SKIP_TESTS_OPEN)
    # The full suite does not run them a second time.
    assert "--ignore=backend/tests/unit/architecture" in skip_block


def test_skip_tests_is_reported_as_skipped_not_passed() -> None:
    skip_block = _block(_script(), SKIP_TESTS_OPEN)
    assert 'skipped "NOUS workflow contract tests + backend unit tests"' in skip_block


def test_frontend_gate_runs_vitest_coverage_and_the_coverage_ratchet() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "run: pnpm test:coverage" in workflow
    assert "check_frontend_coverage.mjs coverage/coverage-summary.json" in workflow

    frontend = _block(_script(), 'if [ "$DO_FRONTEND" -eq 1 ]; then\n')
    assert "( cd frontend && CI=true pnpm test:coverage )" in frontend
    assert 'check $? "pnpm test:coverage"' in frontend
    assert 'COVERAGE_SUMMARY="frontend/coverage/coverage-summary.json"' in frontend
    assert 'node scripts/ci/check_frontend_coverage.mjs "$COVERAGE_SUMMARY"' in frontend
    assert 'check $? "frontend coverage ratchet"' in frontend
    # A stale summary from an earlier run must never be ratcheted.
    assert frontend.index('rm -f -- "$COVERAGE_SUMMARY"') < frontend.index(
        "CI=true pnpm test:coverage"
    )
    assert 'check 1 "frontend coverage ratchet (no coverage summary)"' in frontend
    assert 'skipped "pnpm test:coverage + frontend coverage ratchet"' in frontend


def _plant_run(old: str, new: str) -> Callable[[dict[str, Any]], None]:
    def plant(workflow: dict[str, Any]) -> None:
        step = _unit_test_step(workflow)
        assert old in step["run"]
        step["run"] = step["run"].replace(old, new, 1)

    return plant


def _set_step_env(key: str, value: str) -> Callable[[dict[str, Any]], None]:
    return lambda wf: _unit_test_step(wf)["env"].update({key: value})


WORKFLOW_PLANTS: dict[str, Callable[[dict[str, Any]], None]] = {
    "step env value": _set_step_env("AZURE_OPENAI_API_KEY", "x"),
    "new step env var": _set_step_env("NEW_FLAG", "1"),
    "new workflow env var": lambda wf: wf["env"].update(FEATURE_X="on"),
    "new job env var": lambda wf: wf["jobs"]["unit-tests"].update(env={"JOB_VAR": "1"}),
    "extra -p flag": _plant_run(
        "-c backend/pytest.ini", "-c backend/pytest.ini -p no:randomly"
    ),
    "extra --flag=value": _plant_run(
        "-c backend/pytest.ini", "-c backend/pytest.ini --maxfail=1"
    ),
    "marker expression": _plant_run("or slow)", "or slow or flaky)"),
    "-c path": _plant_run("-c backend/pytest.ini", "-c pytest.ini"),
    "test path": _plant_run("pytest backend/tests/ ", "pytest backend/tests/unit/ "),
}


@pytest.mark.parametrize("name", sorted(WORKFLOW_PLANTS))
def test_a_planted_workflow_divergence_is_reported(name: str) -> None:
    """Mutation guard for the derived checks: each plant must be reported."""
    workflow = copy.deepcopy(_workflow())
    WORKFLOW_PLANTS[name](workflow)
    assert env_drift(workflow, _script()) + pytest_drift(workflow, _script())


def test_a_dropped_local_variable_is_reported() -> None:
    script = _script()
    line = '    AZURE_OPENAI_CHAT_API_VERSION="2024-02-01" \\\n'
    assert line in script
    assert env_drift(_workflow(), script.replace(line, "", 1))


def test_a_ci_only_reporting_change_is_not_drift() -> None:
    workflow = copy.deepcopy(_workflow())
    _plant_run("--cov-fail-under=35", "--cov-fail-under=40")(workflow)
    assert env_drift(workflow, _script()) + pytest_drift(workflow, _script()) == []
