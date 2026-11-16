"""The live OpenAPI route follows the same DEBUG gate as the docs UIs."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROBE_PREFIX = "OPENAPI_DEBUG_GATE="
PROBE = """
import json
from unittest.mock import MagicMock

import tiktoken
from fastapi.testclient import TestClient

# The schema route does not tokenize. Avoid a first-run encoding download.
tiktoken.get_encoding = lambda name: MagicMock()

from src.main import app

client = TestClient(app)
print("OPENAPI_DEBUG_GATE=" + json.dumps({
    "schema": client.get("/openapi.json").status_code,
    "docs": client.get("/docs").status_code,
    "redoc": client.get("/redoc").status_code,
    "generated_paths": bool(app.openapi()["paths"]),
}))
"""


@pytest.mark.parametrize(
    ("debug", "expected_status"),
    [(False, 404), (True, 200)],
)
def test_openapi_http_routes_follow_debug_mode(
    debug: bool, expected_status: int
) -> None:
    env = os.environ.copy()
    env.update(
        {
            "ENVIRONMENT": "testing",
            "DEBUG": str(debug).lower(),
            "DATABASE_URL": "sqlite:///:memory:",
            "TRUSTED_HOSTS": "testserver",
            "AZURE_OPENAI_CHAT_ENDPOINT": "https://example.invalid",
            "AZURE_OPENAI_CHAT_API_KEY": "test-key-not-real",
            "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME": "test-deployment",
            "AZURE_OPENAI_CHAT_API_VERSION": "2024-02-01",
            "AZURE_OPENAI_ENDPOINT": "https://example.invalid",
            "AZURE_OPENAI_API_KEY": "test-key-not-real",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    probe = json.loads(
        next(
            line.removeprefix(PROBE_PREFIX)
            for line in result.stdout.splitlines()
            if line.startswith(PROBE_PREFIX)
        )
    )

    assert probe["schema"] == expected_status
    assert probe["docs"] == expected_status
    assert probe["redoc"] == expected_status
    assert probe["generated_paths"] is True
