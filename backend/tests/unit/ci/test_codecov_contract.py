"""Contract for Codecov's PR coverage status policy."""

from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]  # CI lint gate installs no types-PyYAML

REPO_ROOT = Path(__file__).resolve().parents[4]


def test_new_code_coverage_is_blocking_without_gating_project_coverage() -> None:
    config = cast(
        "dict[str, Any]",
        yaml.safe_load((REPO_ROOT / "codecov.yml").read_text(encoding="utf-8")),
    )

    statuses = config["coverage"]["status"]
    assert statuses["patch"]["default"]["informational"] is False
    assert statuses["patch"]["default"]["target"] == "70%"
    assert statuses["project"]["default"]["informational"] is True
    assert statuses["project"]["default"]["threshold"] == "1%"
    assert config["component_management"]["default_rules"]["statuses"] == [
        {"type": "project", "informational": True}
    ]
