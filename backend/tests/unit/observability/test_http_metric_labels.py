"""HTTP metric labels have bounded cardinality (GOO-654 review).

Once the helpers actually emit, ``request.url.path`` as a label turns every
thread or document id into its own time series, and ``x-user-id`` /
``x-tenant-id`` headers let any caller mint series. Labels use the matched
route template instead, and unmatched paths share one series.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from src.observability import metrics as m
from src.observability.instrumentation import ObservabilityMiddleware

pytestmark = pytest.mark.unit


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(m, "_meter", provider.get_meter("test"))
    monkeypatch.setattr(m, "_metrics_registry", m.MetricsRegistry())
    monkeypatch.setattr(m, "_reported_kind_conflicts", set())
    yield reader


def _attrs(reader: InMemoryMetricReader, name: str) -> List[Dict[str, Any]]:
    data = reader.get_metrics_data()
    out: List[Dict[str, Any]] = []
    for resource_metrics in data.resource_metrics if data else []:
        for scope in resource_metrics.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    out.extend(
                        dict(p.attributes or {}) for p in metric.data.data_points
                    )
    return out


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)

    @app.get("/threads/{thread_id}")
    async def get_thread(thread_id: str) -> Dict[str, str]:
        return {"id": thread_id}

    @app.get("/boom/{item_id}")
    async def boom(item_id: str) -> None:
        raise RuntimeError("bug")

    return TestClient(app, raise_server_exceptions=False)


def test_request_metrics_use_the_route_template(reader: InMemoryMetricReader) -> None:
    client = _client()
    for thread_id in ("t1", "t2", "t3"):
        client.get(f"/threads/{thread_id}", headers={"x-user-id": thread_id})

    points = _attrs(reader, "http_requests_total")
    assert {p["path"] for p in points} == {"/threads/{thread_id}"}
    assert len(points) == 1  # three ids, one series
    assert all("user_id" not in p and "tenant_id" not in p for p in points)


def test_error_metric_uses_the_route_template(reader: InMemoryMetricReader) -> None:
    client = _client()
    client.get("/boom/a")
    client.get("/boom/b")

    points = _attrs(reader, "http_requests_errors_total")
    assert [p["path"] for p in points] == ["/boom/{item_id}"]


def test_unmatched_paths_share_one_series(reader: InMemoryMetricReader) -> None:
    client = _client()
    client.get("/random/probe/1")
    client.get("/random/probe/2")

    paths = {p["path"] for p in _attrs(reader, "http_requests_total")}
    assert paths == {"unmatched"}
