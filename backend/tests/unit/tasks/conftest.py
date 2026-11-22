"""Shared fixtures for Celery task unit tests."""

from __future__ import annotations

from typing import Any, NoReturn

import pytest


@pytest.fixture
def eval_skips(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every evaluation delivery the tasks skip.

    A skipped delivery raises ``Ignore`` (Celery state IGNORED, no SUCCESS),
    so the reason no longer comes back as the task result; this captures the
    response passed to ``_skip_delivery`` while keeping its behaviour.
    """
    from src.tasks import evaluation_tasks as et

    calls: list[dict[str, Any]] = []
    real = et._skip_delivery

    def record(task: Any, response: dict[str, Any]) -> NoReturn:
        calls.append(response)
        real(task, response)

    monkeypatch.setattr(et, "_skip_delivery", record)
    return calls
