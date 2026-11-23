"""Metric helpers must emit, not silently drop (GOO-654, CI2B-1).

Before the fix ``record_histogram``/``increment_counter`` only acted on names
in the pre-registered default list, so HTTP error, Redis, httpx, SLO and
``track_performance`` metrics were never emitted. Before
``configure_metrics()`` the registry was a plain ``dict`` and every call raised
``AttributeError`` (swallowed by callers such as the DO KB ingest path).
``search_results_count`` was created as a Counter but recorded as a histogram.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from src.observability import metrics as m

pytestmark = pytest.mark.unit


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(m, "_meter", provider.get_meter("test"))
    monkeypatch.setattr(m, "_metrics_registry", m.MetricsRegistry())
    monkeypatch.setattr(m, "_reported_kind_conflicts", set())
    yield reader


def _points(reader: InMemoryMetricReader) -> Dict[str, List[Any]]:
    data = reader.get_metrics_data()
    found: Dict[str, List[Any]] = {}
    for resource_metrics in data.resource_metrics if data else []:
        for scope in resource_metrics.scope_metrics:
            for metric in scope.metrics:
                found.setdefault(metric.name, []).extend(metric.data.data_points)
    return found


def test_unregistered_metrics_are_emitted(reader: InMemoryMetricReader) -> None:
    m.increment_counter("http_requests_errors_total", attributes={"code": "500"})
    m.record_histogram("redis_command_duration_seconds", 0.01)
    m.increment_updown_counter("queue_depth_current", 3)

    points = _points(reader)
    assert points["http_requests_errors_total"][0].value == 1
    assert points["redis_command_duration_seconds"][0].count == 1
    assert points["queue_depth_current"][0].value == 3


def test_track_performance_emits_duration_total_and_errors(
    reader: InMemoryMetricReader,
) -> None:
    with m.track_performance("ingest_document"):
        pass
    with pytest.raises(ValueError):
        with m.track_performance("ingest_document"):
            raise ValueError("boom")

    points = _points(reader)
    assert points["ingest_document_duration_seconds"][0].count == 1
    assert points["ingest_document_total"][0].value == 1
    assert points["ingest_document_errors_total"][0].value == 1


def test_search_results_count_is_a_histogram(reader: InMemoryMetricReader) -> None:
    m.initialize_default_metrics()
    m.record_search_metrics("q", result_count=7, duration=0.2, search_type="hybrid")

    points = _points(reader)
    assert points["search_results_count"][0].sum == 7
    assert "search_results_count" in m._metrics_registry.histograms


def test_kind_conflict_is_not_recreated(reader: InMemoryMetricReader) -> None:
    m.increment_counter("jobs_total")
    m.record_histogram("jobs_total", 1.0)  # wrong kind: logged once, dropped

    assert "jobs_total" in m._metrics_registry.counters
    assert "jobs_total" not in m._metrics_registry.histograms
    assert m._reported_kind_conflicts == {"jobs_total"}


def test_helpers_work_before_configure_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Celery worker never calls configure_metrics(); helpers must not raise.
    monkeypatch.setattr(m, "_meter", None)
    monkeypatch.setattr(m, "_metrics_registry", m.MetricsRegistry())

    m.increment_counter("do_kb_ingest_total")
    m.record_histogram("do_kb_ingest_duration_seconds", 0.5)

    assert "do_kb_ingest_total" in m._metrics_registry.counters


def test_module_registry_is_a_registry_at_import() -> None:
    import subprocess
    import sys

    probe = (
        "import src.observability.metrics as m; "
        "assert m._meter is None; "
        "assert isinstance(m._metrics_registry, m.MetricsRegistry); "
        "m.increment_counter('probe_total')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-2000:]
