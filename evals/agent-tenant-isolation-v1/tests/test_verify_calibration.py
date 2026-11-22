"""Every calibration fixture must produce its declared verifier exit code.

The filename prefix is the contract: ``pass-*``/``pass.json`` -> 0,
``wrong-*`` -> 10 (scoreable failure), ``infra-*`` -> 2 (infrastructure,
no reward). ``pass.json`` carries the org-B denials a correctly isolated
backend really returns, so a gate that misreads a real denial as a fault
fails here (GOO-660 review).

Run: pytest -q evals/agent-tenant-isolation-v1/tests/test_verify_calibration.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VERIFY = TESTS_DIR / "verify.py"
FIXTURES = sorted((TESTS_DIR / "calibration").glob("*.json"))
EXPECTED_EXIT = {"pass": 0, "wrong": 10, "infra": 2}


def _expected(fixture: Path) -> int:
    return EXPECTED_EXIT[fixture.stem.split("-", 1)[0]]


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.name for f in FIXTURES])
def test_fixture_exit_code(fixture: Path, tmp_path: Path) -> None:
    env = {
        **os.environ,
        "BENCHMARK_CALIBRATION_FIXTURE": str(fixture),
        "VERIFIER_REPORT_PATH": str(tmp_path / "audit.json"),
    }
    result = subprocess.run(
        [sys.executable, str(VERIFY)], env=env, capture_output=True, text=True
    )
    assert result.returncode == _expected(fixture), result.stdout + result.stderr


def test_pass_fixture_carries_the_real_org_b_denials() -> None:
    """Guards against hiding a denial path behind ``org_b_error: null``."""
    probes = {
        p["probe_id"]: p
        for p in json.loads((TESTS_DIR / "calibration" / "pass.json").read_text())[
            "evidence"
        ]["probes"]
    }
    assert (
        probes["suggestions.load_project_skill"]["org_b_error"]
        == "The project skill snapshot is unavailable for this run."
    )
    assert (
        probes["documents.summarize_document"]["org_b_error"]
        == "Document not found or access denied"
    )
