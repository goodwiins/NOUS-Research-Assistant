"""Quality recommendations must not invent numbers or claim unsaved writes (GOO-653).

* PL-2: both insight analysers built ``QualityInsight(supporting_data=...)`` on
  a dataclass without that field. The TypeError was swallowed, so relevance
  and response-time insights were always empty. The response-time SQL also
  read nonexistent ``search_queries`` columns.
* PL-3: ``get_recommendation_effectiveness`` compared two hard-coded
  dictionaries (0.75 vs 0.65, 800.0 vs 1200.0, ...) and served the result as
  the organization's measured improvement; ``track_recommendation_progress``
  persisted nothing but returned success.
* PL-4: the realtime quality-metrics route streamed simulated scores. No
  client calls it, so it is no longer mounted.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Iterator, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import DataError, IntegrityError, OperationalError, ProgrammingError

from src.services.quality import quality_recommendations_service as svc_module
from src.services.quality.quality_recommendations_service import (
    QualityRecommendationsService,
    RecommendationStatus,
    RecommendationTrackingUnavailable,
)

pytestmark = pytest.mark.unit


def _fake_db(rows: List[Any]) -> Iterator[MagicMock]:
    db = MagicMock()
    results = iter(rows)
    db.execute.side_effect = lambda *_a, **_k: MagicMock(
        fetchone=MagicMock(return_value=next(results))
    )
    yield db


@pytest.mark.asyncio
async def test_relevance_insight_is_produced_and_feeds_recommendations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = SimpleNamespace(
        current_value=0.4, target_value=0.8, sample_count=12, std_dev=0.1
    )
    previous = SimpleNamespace(prev_value=0.5)
    monkeypatch.setattr(
        svc_module, "get_db_sync", lambda: _fake_db([current, previous])
    )
    service = QualityRecommendationsService()

    insights = await service._analyze_relevance_insights(
        "org-1", datetime.utcnow() - timedelta(days=30)
    )

    (insight,) = insights
    assert insight.metric_name == "relevance"
    assert insight.trend == "declining"
    assert insight.supporting_data == {"sample_count": 12, "std_dev": 0.1}

    recommendations = await service._generate_content_recommendations(insights, "org-1")
    assert recommendations
    assert recommendations[0].supporting_data == insight.supporting_data


@pytest.mark.asyncio
async def test_response_time_insight_reads_real_search_query_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: List[str] = []
    current = SimpleNamespace(current_value=2500.0, sample_count=7, p95=4000.0)
    previous = SimpleNamespace(prev_value=2000.0)
    rows = iter([current, previous])

    def _db() -> Iterator[MagicMock]:
        db = MagicMock()

        def _execute(statement: Any, *_a: Any, **_k: Any) -> MagicMock:
            captured.append(str(statement))
            return MagicMock(fetchone=MagicMock(return_value=next(rows)))

        db.execute.side_effect = _execute
        yield db

    monkeypatch.setattr(svc_module, "get_db_sync", _db)

    insights = await QualityRecommendationsService()._analyze_response_time_insights(
        "org-1", datetime.utcnow() - timedelta(days=30)
    )

    (insight,) = insights
    assert insight.metric_name == "response_time"
    assert insight.trend == "declining"
    assert insight.supporting_data == {"sample_count": 7, "p95": 4000.0}
    sql = "\n".join(captured)
    assert "search_duration_ms" in sql
    assert "response_time" not in sql
    assert "search_sessions" not in sql
    assert "sq.organization_id = :org_id" in sql


@pytest.mark.asyncio
async def test_effectiveness_is_reported_unavailable_not_invented() -> None:
    result = await QualityRecommendationsService().get_recommendation_effectiveness(
        "org-1", 30
    )

    assert result["available"] is False
    assert result["effectiveness"] == {}
    assert result["overall_improvement"] is None
    assert result["reason"]


def test_hard_coded_metric_getters_are_gone() -> None:
    service = QualityRecommendationsService()
    assert not hasattr(service, "_get_recent_quality_metrics")
    assert not hasattr(service, "_get_baseline_quality_metrics")


@pytest.mark.asyncio
async def test_progress_tracking_does_not_claim_success() -> None:
    with pytest.raises(RecommendationTrackingUnavailable):
        await QualityRecommendationsService().track_recommendation_progress(
            "rec-1", RecommendationStatus.COMPLETED
        )


@pytest.mark.asyncio
async def test_progress_route_returns_501() -> None:
    from fastapi import HTTPException

    from src.api.quality import quality_recommendations as routes

    with pytest.raises(HTTPException) as caught:
        await routes.update_recommendation_progress(
            "rec-1",
            {"status": "completed"},
            current_user=SimpleNamespace(),  # type: ignore[arg-type]
        )
    assert caught.value.status_code == 501


def test_simulated_realtime_quality_route_is_not_mounted() -> None:
    from src.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert not any(path.startswith("/api/v2/realtime/quality") for path in paths)


def _db_raising(exc: BaseException) -> Any:
    def _factory() -> Iterator[MagicMock]:
        db = MagicMock()
        db.execute.side_effect = exc
        yield db

    return _factory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "analyser", ["_analyze_relevance_insights", "_analyze_response_time_insights"]
)
@pytest.mark.parametrize(
    "bug", [TypeError("bad kwarg"), AttributeError("x"), KeyError("k")]
)
async def test_a_bug_in_an_insight_analyser_is_not_turned_into_no_insights(
    monkeypatch: pytest.MonkeyPatch, analyser: str, bug: Exception
) -> None:
    # The PL-2 TypeError hid for this long because the analysers swallowed
    # every exception and returned []. Programming errors must surface.
    monkeypatch.setattr(svc_module, "get_db_sync", _db_raising(bug))
    with pytest.raises(type(bug)):
        await getattr(QualityRecommendationsService(), analyser)(
            "org-1", datetime.utcnow() - timedelta(days=30)
        )


_ANALYSERS = ["_analyze_relevance_insights", "_analyze_response_time_insights"]


def _down() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("connection refused"))


@pytest.mark.asyncio
@pytest.mark.parametrize("analyser", _ANALYSERS)
async def test_an_unavailable_database_reports_metrics_unavailable(
    monkeypatch: pytest.MonkeyPatch, analyser: str
) -> None:
    monkeypatch.setattr(svc_module, "get_db_sync", _db_raising(_down()))
    with pytest.raises(svc_module._MetricsUnavailable):
        await getattr(QualityRecommendationsService(), analyser)(
            "org-1", datetime.utcnow() - timedelta(days=30)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("analyser", _ANALYSERS)
@pytest.mark.parametrize(
    "error",
    [
        ProgrammingError(
            "SELECT sq.response_time", {}, Exception("column does not exist")
        ),
        DataError("SELECT", {}, Exception("invalid input")),
        IntegrityError("INSERT", {}, Exception("duplicate")),
    ],
)
async def test_a_sql_bug_is_not_read_as_database_unavailable(
    monkeypatch: pytest.MonkeyPatch, analyser: str, error: Exception
) -> None:
    # PL-2's wrong column raised ProgrammingError, an SQLAlchemyError subclass.
    monkeypatch.setattr(svc_module, "get_db_sync", _db_raising(error))
    with pytest.raises(type(error)):
        await getattr(QualityRecommendationsService(), analyser)(
            "org-1", datetime.utcnow() - timedelta(days=30)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("analyser", _ANALYSERS)
async def test_a_failing_session_factory_surfaces_its_own_error(
    monkeypatch: pytest.MonkeyPatch, analyser: str
) -> None:
    # With the session opened inside the try, `finally: db.close()` replaced
    # this with UnboundLocalError.
    def _no_session() -> Iterator[MagicMock]:
        raise RuntimeError("no session")
        yield MagicMock()  # pragma: no cover

    monkeypatch.setattr(svc_module, "get_db_sync", _no_session)
    with pytest.raises(RuntimeError, match="no session"):
        await getattr(QualityRecommendationsService(), analyser)(
            "org-1", datetime.utcnow() - timedelta(days=30)
        )


@pytest.mark.asyncio
async def test_an_outage_returns_no_insights_and_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(svc_module, "get_db_sync", _db_raising(_down()))
    service = QualityRecommendationsService()

    assert await service.analyze_quality_insights("org-1", 30) == []
    assert service.insight_cache == {}  # the outage is not pinned for an hour

    assert await service.generate_recommendations("org-1") == []
    assert service.recommendation_cache == {}


@pytest.mark.asyncio
async def test_complete_insights_are_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc_module, "get_db_sync", lambda: _fake_db([None, None]))
    service = QualityRecommendationsService()

    assert await service.analyze_quality_insights("org-1", 30) == []
    assert list(service.insight_cache) == ["insights_org-1_30"]


def _route_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.quality import quality_recommendations as routes
    from src.core.dependencies import get_current_user

    monkeypatch.setattr(routes.quality_recommendations_service, "insight_cache", {})
    monkeypatch.setattr(
        routes.quality_recommendations_service, "recommendation_cache", {}
    )
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001", organization_id="org-1"
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", ["/insights", "/recommendations"])
def test_an_analyser_bug_is_a_safe_500_logged_once_with_its_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    secret = "user typed this query"

    async def buggy(*_a: Any, **_k: Any) -> List[Any]:
        raise TypeError(secret)

    service = svc_module.quality_recommendations_service
    monkeypatch.setattr(service, "_analyze_relevance_insights", buggy)
    client = _route_client(monkeypatch)

    with caplog.at_level("DEBUG"):
        response = client.get(path)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert secret not in response.text
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1  # once, at the route; services re-raise silently
    (record,) = errors
    assert record.exc_info is not None and record.exc_info[0] is TypeError
    assert secret not in record.getMessage()


@pytest.mark.asyncio
async def test_a_bug_building_an_infrastructure_recommendation_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dashboard = MagicMock()
    # cpu_usage is not a number: comparing it is a bug, not a missing dependency.
    dashboard.get_system_health_metrics = AsyncMock(
        return_value=SimpleNamespace(cpu_usage="high", memory_usage=10.0)
    )
    monkeypatch.setattr(svc_module, "performance_dashboard_service", dashboard)
    with pytest.raises(TypeError):
        await QualityRecommendationsService()._generate_infrastructure_recommendations(
            [], "org-1"
        )


@pytest.mark.asyncio
async def test_an_unavailable_dashboard_still_means_no_infrastructure_recs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dashboard = MagicMock()
    dashboard.get_system_health_metrics = AsyncMock(side_effect=ConnectionError())
    dashboard.get_active_alerts = AsyncMock(side_effect=ConnectionError())
    monkeypatch.setattr(svc_module, "performance_dashboard_service", dashboard)
    service = QualityRecommendationsService()
    assert await service._generate_infrastructure_recommendations([], "org-1") == []
    assert await service._generate_monitoring_recommendations([], "org-1") == []


@pytest.mark.asyncio
async def test_a_bug_building_a_monitoring_recommendation_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dashboard = MagicMock()
    dashboard.get_active_alerts = AsyncMock(return_value=None)  # len(None) is a bug
    monkeypatch.setattr(svc_module, "performance_dashboard_service", dashboard)
    with pytest.raises(TypeError):
        await QualityRecommendationsService()._generate_monitoring_recommendations(
            [], "org-1"
        )
