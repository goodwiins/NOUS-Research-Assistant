"""The evaluation /health 503 body never echoes the exception (GOO-696).

It returned ``"error": str(e)``: the last raw-exception sink in the
evaluation API. The detail is logged instead.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import pytest

from src.api.infrastructure import evaluation as ev

pytestmark = pytest.mark.unit

SECRET = "redis://admin:hunter2@10.0.0.5:6379 refused"


class _FailingOnceClock:
    """``datetime`` stand-in whose first ``utcnow()`` raises."""

    def __init__(self) -> None:
        self.calls = 0

    def utcnow(self) -> datetime:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError(SECRET)
        return datetime(2026, 11, 22)


async def test_health_503_uses_a_fixed_message_and_logs_the_detail(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ev, "datetime", _FailingOnceClock())

    with caplog.at_level(logging.ERROR, logger=ev.logger.name):
        response: Any = await ev.evaluation_health_check()

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["status"] == "unhealthy"
    assert body["error"] == "Evaluation service health check failed"
    assert "hunter2" not in response.body.decode()
    assert "hunter2" in caplog.text
