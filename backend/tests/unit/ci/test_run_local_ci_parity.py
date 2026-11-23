"""run_local_ci.sh must run the gates CI runs, the way CI runs them (GOO-738).

Each contract below is a gap that let a branch pass locally and fail in CI:
backend tests run from ``backend/`` instead of the repo root, architecture
tests skipped with ``--skip-tests``, and no frontend coverage ratchet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_backend_marker_expression_matches_the_workflow() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    markers = "unit or not (integration or e2e or slow)"
    assert f'-m "{markers}"' in workflow
    assert f'BACKEND_UNIT_MARKERS="{markers}"' in _script()


def test_backend_tests_get_the_unit_job_placeholder_env() -> None:
    script = _script()
    start = script.index("backend_pytest() {")
    helper = script[start : script.index("\n}\n", start)]
    for assignment in (
        "ENVIRONMENT=testing",
        'DATABASE_URL="sqlite:///:memory:"',
        "AZURE_OPENAI_CHAT_ENDPOINT=",
        "AZURE_OPENAI_API_KEY=",
    ):
        assert assignment in helper


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
