"""Render the frontend views under node with a minimal DOM (dom_harness.js),
so a test can check what the page shows without a browser."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
NODE = shutil.which("node")


def render(view: str, data: Any, tmp_path: Path) -> dict:
    if NODE is None:
        pytest.skip("node is not installed")
    src = tmp_path / f"view-{view}.json"
    src.write_text(json.dumps(data))
    out = subprocess.run(
        [NODE, str(HERE / "dom_harness.js"), view, str(src)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    result: dict = json.loads(out.stdout)
    return result
