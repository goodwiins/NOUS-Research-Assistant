"""Guard the Test Pipeline concurrency policy used by release promotion."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_test_pipeline_only_cancels_superseded_pull_requests() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "test-pipeline.yml").read_text(
            encoding="utf-8"
        )
    )
    concurrency = workflow["concurrency"]

    # Keep PR-number/branch grouping; a push to develop must finish so it can
    # trigger release promotion even if another push arrives while it runs.
    assert concurrency["group"] == (
        "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}"
    )
    assert concurrency["cancel-in-progress"] == (
        "${{ github.event_name == 'pull_request' }}"
    )
