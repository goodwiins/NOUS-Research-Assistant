"""Contract tests for the local frontend release-gate stages."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "run_local_ci.sh"


def test_frontend_gate_matches_the_workflow_ratchet_contract() -> None:
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    assert (
        '( cd frontend && pnpm run type-check ); check $? "pnpm type-check"' in script
    )
    assert (
        'pnpm exec eslint app src --format json --output-file "$ESLINT_REPORT"'
        in script
    )
    assert (
        'node scripts/ci/check_frontend_quality.mjs --report "$ESLINT_REPORT" '
        '--base "$BASE"'
    ) in script
    assert 'scripts/ci/check_tsconfig_exclusions.py --base "$BASE"' in script
    assert 'check $? "frontend quality ratchet"' in script
    assert 'check $? "tsconfig exclusion ratchet"' in script
    # GOO-771: BSD mktemp randomises only trailing Xs, so the report gets a
    # `mktemp -d` directory whose template ends in Xs, removed by a trap.
    # tests/unit/scripts/test_nous_loop_contract.py runs it with a stale file.
    assert (
        'ESLINT_REPORT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/run_local_ci-eslint.XXXXXX")"'
        in script
    )
    assert "XXXXXX.json" not in script
    assert "trap _remove_eslint_report_dir EXIT" in script
    assert 'rm -rf -- "$ESLINT_REPORT_DIR"' in script
    assert "full lint" in script.lower()
    assert "advisory" in script.lower()
    assert "FULL_LINT_RC=$?" in script
    assert "pnpm lint (advisory)" in script
    assert '( cd frontend && pnpm run lint );       check $? "pnpm lint"' not in script
