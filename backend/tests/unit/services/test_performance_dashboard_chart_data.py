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

import logging
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


@pytest.mark.asyncio
async def test_health_check_bug_propagates_and_the_fallback_has_no_error_rate() -> None:
    service = PerformanceDashboardService()
    bug = ProgrammingError("SELECT", {}, Exception("column does not exist"))

    with pytest.raises(ProgrammingError):
        await service._get_db_health_metrics(db=cast(Any, _FakeDB(bug)))

    # get_system_health_metrics' own fallback must not invent a 0.0 either.
    health = await service.get_system_health_metrics(db=cast(Any, _FakeDB(bug)))
    assert health.error_rate is None


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
