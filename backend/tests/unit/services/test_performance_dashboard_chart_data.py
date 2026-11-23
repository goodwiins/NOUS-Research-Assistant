"""Chart data reports outages and the granularity it used; no fake zeros.

GOO-732:
* ``get_metric_chart_data`` returned ``[]`` when its query failed, so a
  database outage was drawn as an empty chart. It now raises
  ``MetricsUnavailable`` for connection-level failures only (the GOO-653
  rule), the routes answer 503, and any other error is a bug that surfaces.
* The service buckets by week for any range past 7d, but the route echoed
  the caller's ``granularity`` (``"day"``), so weekly totals were labelled
  daily. The response now reports the granularity actually used.
* ``avg_response_time`` was 0.0 with no searches, and ``error_rate`` was
  0.0 always (a literal ``SELECT 0 as errors``) and on failure. Both are null
  when there is no measurement.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from typing import Any, AsyncIterator, List, Optional, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import DataError, OperationalError, ProgrammingError

from src.api.quality import performance_dashboard as pd
from src.models.user import User, UserRole
from src.services.quality import performance_dashboard_service as svc_module
from src.services.quality.performance_dashboard_service import (
    MetricsUnavailable,
    MetricTimeRange,
    PerformanceDashboardService,
    chart_granularity,
)

pytestmark = pytest.mark.unit

ORG = str(uuid4())


def _operational() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("connection refused"))


class _Result:
    def __init__(self, rows: List[Any]) -> None:
        self.rows = rows

    def fetchall(self) -> List[Any]:
        return self.rows

    def fetchone(self) -> Optional[Any]:
        return self.rows[0] if self.rows else None


class _FakeDB:
    """Hands out queued results; raises a queued exception instead."""

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.statements: List[str] = []

    async def execute(self, statement: Any, params: Any = None) -> _Result:
        self.statements.append(str(statement))
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _Result(item)


def _patch_session(
    monkeypatch: pytest.MonkeyPatch, db: Any, open_error: Optional[Exception] = None
) -> None:
    @asynccontextmanager
    async def _session() -> AsyncIterator[Any]:
        if open_error is not None:
            raise open_error
        yield db

    monkeypatch.setattr(svc_module, "get_async_session", _session)


def _user() -> User:
    return cast(
        User, SimpleNamespace(id=uuid4(), organization_id=uuid4(), role=UserRole.USER)
    )


# --- get_metric_chart_data ------------------------------------------------


@pytest.mark.asyncio
async def test_chart_returns_rows_bucketed_by_the_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period = datetime(2026, 11, 16)
    db = _FakeDB([SimpleNamespace(period=period, value=7)])
    _patch_session(monkeypatch, db)

    data = await PerformanceDashboardService().get_metric_chart_data(
        ORG, "search_volume", MetricTimeRange.LAST_30D, granularity="day"
    )

    assert data == [{"timestamp": period.isoformat(), "value": 7.0}]
    assert "DATE_TRUNC('week'" in db.statements[0]


@pytest.mark.asyncio
async def test_chart_outage_is_unavailable_not_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_session(monkeypatch, _FakeDB(_operational()))

    with pytest.raises(MetricsUnavailable):
        await PerformanceDashboardService().get_metric_chart_data(
            ORG, "search_volume", MetricTimeRange.LAST_24H
        )


@pytest.mark.asyncio
async def test_chart_outage_while_opening_the_session_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_session(monkeypatch, _FakeDB(), open_error=_operational())

    with pytest.raises(MetricsUnavailable):
        await PerformanceDashboardService().get_metric_chart_data(
            ORG, "response_time", MetricTimeRange.LAST_7D
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ProgrammingError("SELECT", {}, Exception("column does not exist")),
        DataError("SELECT", {}, Exception("bad uuid")),
        TypeError("bug"),
    ],
)
async def test_chart_bugs_propagate(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _patch_session(monkeypatch, _FakeDB(error))

    with pytest.raises(type(error)):
        await PerformanceDashboardService().get_metric_chart_data(
            ORG, "quality_score", MetricTimeRange.LAST_24H
        )


@pytest.mark.parametrize(
    "time_range,expected",
    [
        (MetricTimeRange.LAST_HOUR, "hour"),
        (MetricTimeRange.LAST_24H, "hour"),
        (MetricTimeRange.LAST_7D, "day"),
        (MetricTimeRange.LAST_30D, "week"),
        (MetricTimeRange.LAST_90D, "week"),
    ],
)
def test_chart_granularity(time_range: MetricTimeRange, expected: str) -> None:
    assert chart_granularity(time_range) == expected


# --- the chart route ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "time_range,expected",
    [(MetricTimeRange.LAST_30D, "week"), (MetricTimeRange.LAST_7D, "day")],
)
async def test_route_reports_the_granularity_used_not_the_one_asked(
    time_range: MetricTimeRange, expected: str
) -> None:
    with patch.object(
        pd.performance_dashboard_service,
        "get_metric_chart_data",
        new=AsyncMock(return_value=[]),
    ):
        body = await pd.get_metric_chart_data(
            metric_name="search_volume",
            time_range=time_range,
            granularity="minute",
            current_user=_user(),
        )

    assert body["granularity"] == expected


@pytest.mark.asyncio
async def test_route_answers_503_when_metrics_are_unavailable() -> None:
    with patch.object(
        pd.performance_dashboard_service,
        "get_metric_chart_data",
        new=AsyncMock(side_effect=MetricsUnavailable()),
    ):
        with pytest.raises(HTTPException) as raised:
            await pd.get_metric_chart_data(
                metric_name="search_volume",
                time_range=MetricTimeRange.LAST_24H,
                granularity="hour",
                current_user=_user(),
            )

    assert raised.value.status_code == 503
    assert raised.value.detail == "Metrics are temporarily unavailable"


@pytest.mark.asyncio
async def test_route_bug_is_a_safe_500_logged_once_with_its_stack(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with patch.object(
        pd.performance_dashboard_service,
        "get_metric_chart_data",
        new=AsyncMock(side_effect=TypeError("secret detail")),
    ):
        with caplog.at_level(logging.ERROR, logger=pd.logger.name):
            with pytest.raises(HTTPException) as raised:
                await pd.get_metric_chart_data(
                    metric_name="search_volume",
                    time_range=MetricTimeRange.LAST_24H,
                    granularity="hour",
                    current_user=_user(),
                )

    assert raised.value.status_code == 500
    assert raised.value.detail == "Internal server error"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert "secret detail" not in errors[0].getMessage()


# --- widgets ----------------------------------------------------------------


_WIDGET = {
    "title": "Searches",
    "widget_type": "stat_card",
    "metrics": ["search_volume"],
    "time_range": "24h",
    "position": {"x": 0, "y": 0},
    "size": {"w": 1, "h": 1},
}


@pytest.mark.asyncio
async def test_widget_outage_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PerformanceDashboardService()
    monkeypatch.setattr(
        service, "get_metric_chart_data", AsyncMock(side_effect=MetricsUnavailable())
    )

    with pytest.raises(MetricsUnavailable):
        await service.create_dashboard_widgets(ORG, [dict(_WIDGET)])


@pytest.mark.asyncio
async def test_widgets_route_answers_503_when_metrics_are_unavailable() -> None:
    with patch.object(
        pd.performance_dashboard_service,
        "create_dashboard_widgets",
        new=AsyncMock(side_effect=MetricsUnavailable()),
    ):
        with pytest.raises(HTTPException) as raised:
            await pd.create_dashboard_widgets(
                widget_configs=[dict(_WIDGET)], current_user=_user()
            )

    assert raised.value.status_code == 503


# --- "no measurement" is null ---------------------------------------------


def _search_rows(total: int, avg: Optional[float]) -> List[Any]:
    return [
        [
            SimpleNamespace(
                total_searches=total,
                avg_response_time=avg,
                no_results_count=0,
                error_count=0,
            )
        ],
        [SimpleNamespace(p95=None)],
        [SimpleNamespace(query="q", search_count=1, avg_response_time=None)],
        [],
    ]


@pytest.mark.asyncio
async def test_avg_response_time_is_null_without_timed_searches() -> None:
    db = _FakeDB(*_search_rows(0, None))

    metrics = await PerformanceDashboardService().get_search_performance_metrics(
        ORG, MetricTimeRange.LAST_24H, db=cast(Any, db)
    )

    assert metrics.avg_response_time is None
    assert metrics.top_queries[0]["avg_response_time"] is None


@pytest.mark.asyncio
async def test_avg_response_time_is_reported_when_measured() -> None:
    db = _FakeDB(*_search_rows(4, 120.5))

    metrics = await PerformanceDashboardService().get_search_performance_metrics(
        ORG, MetricTimeRange.LAST_24H, db=cast(Any, db)
    )

    assert metrics.avg_response_time == 120.5


@pytest.mark.asyncio
async def test_error_rate_is_null_because_nothing_measures_it() -> None:
    db = _FakeDB(
        [SimpleNamespace(p50=10.0, p95=20.0)],
        [SimpleNamespace(active_sessions=2)],
    )

    metrics = await PerformanceDashboardService()._get_db_health_metrics(
        db=cast(Any, db)
    )

    assert metrics["error_rate"] is None
    assert metrics["p50"] == 10.0
    assert not any("0 as errors" in s for s in db.statements)


@pytest.mark.asyncio
async def test_error_rate_is_null_when_the_health_check_cannot_connect() -> None:
    metrics = await PerformanceDashboardService()._get_db_health_metrics(
        db=cast(Any, _FakeDB(_operational()))
    )

    assert metrics["error_rate"] is None


# --- system health: real host values, null for what failed ---------------

HOST = SimpleNamespace(
    cpu=37.5, memory=61.0, disk=44.0, boot=1_000.0, sent=10, recv=20, ps=3, pr=4
)


def _fake_psutil(fail: Optional[BaseException] = None) -> Any:
    def _cpu(interval: Any = None) -> float:
        if fail is not None:
            raise fail
        return float(HOST.cpu)

    return SimpleNamespace(
        cpu_percent=_cpu,
        virtual_memory=lambda: SimpleNamespace(percent=HOST.memory),
        disk_usage=lambda _path: SimpleNamespace(percent=HOST.disk),
        net_io_counters=lambda: SimpleNamespace(
            bytes_sent=HOST.sent,
            bytes_recv=HOST.recv,
            packets_sent=HOST.ps,
            packets_recv=HOST.pr,
        ),
        boot_time=lambda: HOST.boot,
        Error=_PsutilError,
    )


class _PsutilError(Exception):
    pass


def _with_psutil(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr(svc_module, "PSUTIL_AVAILABLE", True)
    monkeypatch.setattr(svc_module, "psutil", fake, raising=False)


@pytest.mark.asyncio
async def test_db_outage_keeps_the_real_host_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_psutil(monkeypatch, _fake_psutil())

    health = await PerformanceDashboardService().get_system_health_metrics(
        db=cast(Any, _FakeDB(_operational()))
    )

    assert (health.cpu_usage, health.memory_usage, health.disk_usage) == (
        HOST.cpu,
        HOST.memory,
        HOST.disk,
    )
    assert health.network_io == {
        "bytes_sent": 10.0,
        "bytes_recv": 20.0,
        "packets_sent": 3.0,
        "packets_recv": 4.0,
    }
    assert health.uptime is not None and health.uptime > 0
    # Only the DB-derived fields are null.
    assert health.response_time_p50 is None
    assert health.response_time_p95 is None
    assert health.error_rate is None
    assert health.active_connections is None


@pytest.mark.asyncio
async def test_health_check_bug_propagates_with_no_fallback_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_psutil(monkeypatch, _fake_psutil())
    bug = ProgrammingError("SELECT", {}, Exception("column does not exist"))

    with pytest.raises(ProgrammingError):
        await PerformanceDashboardService().get_system_health_metrics(
            db=cast(Any, _FakeDB(bug))
        )


@pytest.mark.asyncio
async def test_system_health_route_bug_is_a_safe_500_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bug = ProgrammingError("SELECT", {}, Exception("column secret_col does not exist"))
    with patch.object(
        pd.performance_dashboard_service,
        "get_system_health_metrics",
        new=AsyncMock(side_effect=bug),
    ):
        with caplog.at_level(logging.ERROR, logger=pd.logger.name):
            with pytest.raises(HTTPException) as raised:
                await pd.get_system_health_metrics(current_user=_user())

    assert raised.value.status_code == 500
    assert "secret_col" not in str(raised.value.detail)
    logged = [r for r in caplog.records if r.name == pd.logger.name]
    assert len(logged) == 1 and logged[0].exc_info is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError("disk gone"), _PsutilError("denied")])
async def test_psutil_failure_nulls_only_the_host_fields(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    _with_psutil(monkeypatch, _fake_psutil(fail=failure))
    db = _FakeDB(
        [SimpleNamespace(p50=10.0, p95=20.0)], [SimpleNamespace(active_sessions=2)]
    )

    health = await PerformanceDashboardService().get_system_health_metrics(
        db=cast(Any, db)
    )

    assert health.cpu_usage is None and health.memory_usage is None
    assert health.disk_usage is None and health.uptime is None
    assert health.network_io is None
    assert (health.response_time_p50, health.active_connections) == (10.0, 2)


@pytest.mark.asyncio
async def test_without_psutil_host_fields_are_null_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(svc_module, "PSUTIL_AVAILABLE", False)
    db = _FakeDB(
        [SimpleNamespace(p50=None, p95=None)], [SimpleNamespace(active_sessions=0)]
    )

    health = await PerformanceDashboardService().get_system_health_metrics(
        db=cast(Any, db)
    )

    assert health.cpu_usage is None and health.uptime is None
    assert health.network_io is None
    # No timed search in the window: no percentile, not 0 ms.
    assert health.response_time_p50 is None and health.response_time_p95 is None
    assert health.active_connections == 0


@pytest.mark.asyncio
async def test_recommendations_skip_unmeasured_host_values() -> None:
    from src.services.quality import quality_recommendations_service as qr

    health = SimpleNamespace(cpu_usage=None, memory_usage=90.0)
    with patch.object(
        qr.performance_dashboard_service,
        "get_system_health_metrics",
        new=AsyncMock(return_value=health),
    ):
        recs = await qr.QualityRecommendationsService()._generate_infrastructure_recommendations(
            [], ORG
        )

    # An unknown CPU is skipped; it does not abort the memory check.
    titles = [r.title for r in recs]
    assert len(titles) == 1 and "Memory" in titles[0]


# --- what counts as "unavailable" -------------------------------------------


def _unavailable_errors() -> List[BaseException]:
    from sqlalchemy.exc import DisconnectionError, InterfaceError
    from sqlalchemy.exc import TimeoutError as PoolTimeoutError

    return [
        _operational(),
        InterfaceError("SELECT 1", {}, Exception("connection closed")),
        DisconnectionError("connection invalidated"),
        PoolTimeoutError("QueuePool limit reached"),
        ConnectionRefusedError(111, "Connect call failed"),
        socket.gaierror(-2, "Name or service not known"),
        asyncio.TimeoutError(),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", _unavailable_errors(), ids=lambda e: type(e).__name__)
async def test_chart_session_open_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    # asyncpg raises these when the pool opens a connection.
    _patch_session(monkeypatch, _FakeDB(), open_error=cast(Exception, error))

    with pytest.raises(MetricsUnavailable):
        await PerformanceDashboardService().get_metric_chart_data(
            ORG, "search_volume", MetricTimeRange.LAST_24H
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("error", _unavailable_errors(), ids=lambda e: type(e).__name__)
async def test_db_health_connection_failures_null_the_db_fields(
    error: BaseException,
) -> None:
    metrics = await PerformanceDashboardService()._get_db_health_metrics(
        db=cast(Any, _FakeDB(error))
    )

    assert metrics == {
        "p50": None,
        "p95": None,
        "error_rate": None,
        "active_connections": None,
    }


# --- route details ----------------------------------------------------------


def test_csv_export_keeps_null_metrics_as_empty_cells() -> None:
    csv = pd._convert_to_csv(
        {"system_health": {"error_rate": None, "cpu_usage": 12.5}, "label": "x"}
    )

    assert ["system_health_error_rate", ""] in csv["rows"]
    assert ["system_health_cpu_usage", "12.5"] in csv["rows"]
    assert all(row[0] != "label" for row in csv["rows"])


@pytest.mark.asyncio
async def test_error_rate_is_not_offered_as_a_chart() -> None:
    with pytest.raises(HTTPException) as raised:
        await pd.get_metric_chart_data(
            metric_name="error_rate",
            time_range=MetricTimeRange.LAST_24H,
            granularity="hour",
            current_user=_user(),
        )

    assert raised.value.status_code == 400


@pytest.mark.asyncio
async def test_member_overview_still_hides_system_health() -> None:
    overview = {
        "system_health": {"error_rate": None},
        "search_performance": {"avg_response_time": None},
    }
    with patch.object(
        pd.performance_dashboard_service,
        "get_dashboard_overview",
        new=AsyncMock(return_value=overview),
    ):
        body = await pd.get_dashboard_overview(
            time_range=MetricTimeRange.LAST_24H, current_user=_user()
        )

    assert "system_health" not in body
    assert body["search_performance"] == {"avg_response_time": None}
