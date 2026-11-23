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
from unittest.mock import MagicMock

import pytest

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
async def test_relevance_insight_is_produced_and_feeds_recommendations(monkeypatch):
    current = SimpleNamespace(
        current_value=0.4, target_value=0.8, sample_count=12, std_dev=0.1
    )
    previous = SimpleNamespace(prev_value=0.5)
    monkeypatch.setattr(svc_module, "get_db_sync", lambda: _fake_db([current, previous]))
    service = QualityRecommendationsService()

    insights = await service._analyze_relevance_insights(
        "org-1", datetime.utcnow() - timedelta(days=30)
    )

    (insight,) = insights
    assert insight.metric_name == "relevance"
    assert insight.trend == "declining"
    assert insight.supporting_data == {"sample_count": 12, "std_dev": 0.1}

    recommendations = await service._generate_content_recommendations(
        insights, "org-1"
    )
    assert recommendations
    assert recommendations[0].supporting_data == insight.supporting_data


@pytest.mark.asyncio
async def test_response_time_insight_reads_real_search_query_columns(monkeypatch):
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
async def test_effectiveness_is_reported_unavailable_not_invented():
    result = await QualityRecommendationsService().get_recommendation_effectiveness(
        "org-1", 30
    )

    assert result["available"] is False
    assert result["effectiveness"] == {}
    assert result["overall_improvement"] is None
    assert result["reason"]


def test_hard_coded_metric_getters_are_gone():
    service = QualityRecommendationsService()
    assert not hasattr(service, "_get_recent_quality_metrics")
    assert not hasattr(service, "_get_baseline_quality_metrics")


@pytest.mark.asyncio
async def test_progress_tracking_does_not_claim_success():
    with pytest.raises(RecommendationTrackingUnavailable):
        await QualityRecommendationsService().track_recommendation_progress(
            "rec-1", RecommendationStatus.COMPLETED
        )


@pytest.mark.asyncio
async def test_progress_route_returns_501(monkeypatch):
    from fastapi import HTTPException

    from src.api.quality import quality_recommendations as routes

    with pytest.raises(HTTPException) as caught:
        await routes.update_recommendation_progress(
            "rec-1", {"status": "completed"}, current_user=SimpleNamespace()
        )
    assert caught.value.status_code == 501


def test_simulated_realtime_quality_route_is_not_mounted():
    from src.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert not any(path.startswith("/api/v2/realtime/quality") for path in paths)
