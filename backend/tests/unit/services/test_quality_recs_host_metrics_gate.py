"""Host CPU/memory never reach a regular member via recommendations (GOO-672).

#2197 limited host metrics in the performance overview to platform operators.
``QualityRecommendationsService._generate_infrastructure_recommendations`` was
a second path: once host CPU > 80% or memory > 85% it added recommendations
whose description and ``supporting_data`` carried the host figures, shown to
any org member through ``/recommendations``, ``/summary`` and ``/generate``.
Those host-derived recommendations are now opt-in, and the routes opt in only
for platform operators.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, List, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks

from src.api.quality import quality_recommendations as routes
from src.core import dependencies
from src.models.user import User, UserRole
from src.services.quality import quality_recommendations_service as svc_mod
from src.services.quality.quality_recommendations_service import (
    QualityRecommendationsService,
)

pytestmark = pytest.mark.unit

_HOT_HOST = SimpleNamespace(cpu_usage=97.5, memory_usage=93.25)


def _mentions_host_figures(recs: List[Any]) -> bool:
    for rec in recs:
        blob = f"{rec.description} {rec.supporting_data}"
        if "97.5" in blob or "93.2" in blob:
            return True
        if {"cpu_usage", "memory_usage"} & set(rec.supporting_data or {}):
            return True
    return False


@pytest.fixture
def hot_host() -> Iterator[AsyncMock]:
    health = AsyncMock(return_value=_HOT_HOST)
    with (
        patch.object(
            svc_mod.performance_dashboard_service,
            "get_system_health_metrics",
            new=health,
        ),
        patch.object(
            svc_mod.performance_dashboard_service,
            "get_active_alerts",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            QualityRecommendationsService,
            "analyze_quality_insights",
            new=AsyncMock(return_value=[]),
        ),
    ):
        yield health


@pytest.mark.asyncio
async def test_default_recommendations_omit_host_metrics(hot_host: AsyncMock) -> None:
    recs = await QualityRecommendationsService().generate_recommendations(
        organization_id=str(uuid4())
    )
    assert not _mentions_host_figures(recs)
    hot_host.assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_opt_in_still_gets_host_recommendations(
    hot_host: AsyncMock,
) -> None:
    recs = await QualityRecommendationsService().generate_recommendations(
        organization_id=str(uuid4()), include_host_metrics=True
    )
    assert _mentions_host_figures(recs)


@pytest.mark.asyncio
async def test_operator_result_is_not_served_from_cache_to_members(
    hot_host: AsyncMock,
) -> None:
    service = QualityRecommendationsService()
    org = str(uuid4())
    await service.generate_recommendations(
        organization_id=org, include_host_metrics=True
    )
    member_view = await service.generate_recommendations(organization_id=org)
    assert not _mentions_host_figures(member_view)


def _user(role: UserRole) -> User:
    return cast(User, SimpleNamespace(id=uuid4(), organization_id=uuid4(), role=role))


@pytest.mark.parametrize("role", [UserRole.USER, UserRole.ADMIN])
@pytest.mark.asyncio
async def test_member_routes_never_return_host_metrics(
    hot_host: AsyncMock, role: UserRole
) -> None:
    """Every route that serves recommendations, called by a non-operator."""
    user = _user(role)
    listed = await routes.get_quality_recommendations(
        categories=None,
        priority=None,
        status=None,
        days_back=30,
        limit=50,
        current_user=user,
    )
    generated = await routes.generate_custom_recommendations(
        request_data={}, background_tasks=BackgroundTasks(), current_user=user
    )
    summary = await routes.get_recommendations_summary(days_back=30, current_user=user)
    for body in (listed, generated, summary):
        text = str(body)
        assert "97.5" not in text and "93.2" not in text
    hot_host.assert_not_awaited()


@pytest.mark.asyncio
async def test_platform_operator_route_includes_host_metrics(
    hot_host: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = _user(UserRole.USER)
    monkeypatch.setattr(
        dependencies.settings, "PLATFORM_OPERATOR_USER_IDS", str(operator.id)
    )
    body = await routes.get_quality_recommendations(
        categories=None,
        priority=None,
        status=None,
        days_back=30,
        limit=50,
        current_user=operator,
    )
    assert "97.5" in str(body)


def test_routes_pass_the_operator_gate() -> None:
    source = inspect.getsource(routes)
    assert source.count("include_host_metrics=is_platform_operator(current_user)") == 3
