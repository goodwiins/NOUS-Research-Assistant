"""GET /workers/health is a typed contract (GOO-718).

The Trigger.dev health monitor parses this response with a zod schema
(``src/trigger/_lib/schemas.ts``). Without a ``response_model`` the OpenAPI
snapshot declared it as ``{}``, so a shape change could not fail CI. The
Trigger side is pinned by ``tests/trigger/worker-response-contract.test.mjs``.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi.routing import APIRoute

pytestmark = pytest.mark.unit


def _fake_celery(
    active: Optional[dict[str, Any]], stats: Optional[dict[str, Any]]
) -> SimpleNamespace:
    inspect = SimpleNamespace(active=lambda: active, stats=lambda: stats)
    control = SimpleNamespace(inspect=lambda: inspect)
    return SimpleNamespace(control=control)


def _install_celery(monkeypatch: pytest.MonkeyPatch, celery_app: Any) -> None:
    monkeypatch.setitem(
        sys.modules,
        "src.tasks.processing_tasks",
        SimpleNamespace(celery_app=celery_app),
    )


def test_health_route_declares_a_response_model() -> None:
    from src.api.infrastructure.workers import WorkerHealthResponse, router

    routes = {r.path: r for r in router.routes if isinstance(r, APIRoute)}
    assert routes["/workers/health"].response_model is WorkerHealthResponse


def test_health_model_keeps_the_existing_wire_fields() -> None:
    from src.api.infrastructure.workers import WorkerHealthResponse

    schema = WorkerHealthResponse.model_json_schema()
    assert set(schema["required"]) == {
        "healthy",
        "workers_online",
        "issues",
        "timestamp",
    }
    assert schema["properties"]["healthy"]["type"] == "boolean"
    assert schema["properties"]["workers_online"]["type"] == "integer"
    assert schema["properties"]["issues"]["items"]["type"] == "string"
    assert schema["properties"]["timestamp"]["type"] == "string"


def test_healthy_workers_produce_a_valid_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.infrastructure.workers import WorkerHealthResponse, get_workers_health

    _install_celery(
        monkeypatch,
        _fake_celery(
            active={"w1@host": []},
            stats={"w1@host": {"pool": {"max-concurrency": 4}}},
        ),
    )
    body = WorkerHealthResponse.model_validate(
        get_workers_health(current_user=None)  # type: ignore[arg-type]
    )
    assert body.healthy is True
    assert body.workers_online == 1
    assert body.issues == []


def test_unreachable_workers_still_produce_a_valid_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.infrastructure.workers import WorkerHealthResponse, get_workers_health

    def _boom() -> None:
        raise RuntimeError("broker down")

    _install_celery(
        monkeypatch, SimpleNamespace(control=SimpleNamespace(inspect=_boom))
    )
    body = WorkerHealthResponse.model_validate(
        get_workers_health(current_user=None)  # type: ignore[arg-type]
    )
    assert body.healthy is False
    assert body.workers_online == 0
    assert body.issues == ["Failed to check worker health"]
