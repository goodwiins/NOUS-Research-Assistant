"""Performance dashboard sections follow the same gates as their own routes.

GOO-633 (finding API2A-1): ``/system-health`` and ``/user-engagement`` were
admin-gated, but ``/overview`` and ``/export`` returned the whole overview,
host metrics and per-user ``top_users`` included, to any org member. Host
metrics describe the shared deployment, so they are now platform-operator
only everywhere; user engagement stays org-admin only everywhere.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from src.api.quality import performance_dashboard as pd
from src.core import dependencies
from src.core.dependencies import require_platform_operator
from src.models.user import UserRole
from src.services.quality.performance_dashboard_service import MetricTimeRange

pytestmark = pytest.mark.unit


def _overview() -> Dict[str, Any]:
    return {
        "timestamp": "2026-11-21T00:00:00",
        "time_range": "24h",
        "system_health": {"cpu_usage": 42.0},
        "search_performance": {"total_searches": 3},
        "quality_metrics": {"overall_score": 0.9},
        "user_engagement": {"top_users": [{"user_id": "u", "name": "n"}]},
        "alerts": [],
    }


def _user(role: UserRole) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), organization_id=uuid4(), role=role)


@pytest.fixture
def overview_mock():  # type: ignore[no-untyped-def]
    cached = _overview()
    with patch.object(
        pd.performance_dashboard_service,
        "get_dashboard_overview",
        new=AsyncMock(return_value=cached),
    ):
        yield cached


def _make_operator(monkeypatch: pytest.MonkeyPatch, user: SimpleNamespace) -> None:
    monkeypatch.setattr(
        dependencies.settings, "PLATFORM_OPERATOR_USER_IDS", str(user.id)
    )


@pytest.mark.asyncio
async def test_member_overview_omits_host_and_engagement(overview_mock) -> None:  # type: ignore[no-untyped-def]
    body = await pd.get_dashboard_overview(
        time_range=MetricTimeRange.LAST_24H, current_user=_user(UserRole.USER)
    )
    assert "system_health" not in body
    assert "user_engagement" not in body
    assert body["search_performance"] == {"total_searches": 3}
    # The cached overview object must not be mutated by redaction.
    assert "system_health" in overview_mock


@pytest.mark.asyncio
async def test_member_export_omits_host_and_engagement(overview_mock) -> None:  # type: ignore[no-untyped-def]
    body = await pd.export_dashboard_data(
        format="json",
        time_range=MetricTimeRange.LAST_24H,
        metrics=["system_health", "user_engagement", "quality_metrics"],
        current_user=_user(UserRole.USER),
    )
    assert "system_health" not in body
    assert "user_engagement" not in body
    assert body["quality_metrics"] == {"overall_score": 0.9}


@pytest.mark.asyncio
async def test_member_csv_export_omits_host_and_engagement(overview_mock) -> None:  # type: ignore[no-untyped-def]
    response = await pd.export_dashboard_data(
        format="csv",
        time_range=MetricTimeRange.LAST_24H,
        metrics=[],
        current_user=_user(UserRole.USER),
    )
    keys = {row[0] for row in json.loads(response.body)["rows"]}
    assert not any(k.startswith(("system_health", "user_engagement")) for k in keys)


@pytest.mark.asyncio
async def test_org_admin_gets_engagement_but_not_host_metrics(overview_mock) -> None:  # type: ignore[no-untyped-def]
    body = await pd.get_dashboard_overview(
        time_range=MetricTimeRange.LAST_24H, current_user=_user(UserRole.ADMIN)
    )
    assert "user_engagement" in body
    assert "system_health" not in body


@pytest.mark.asyncio
async def test_platform_operator_gets_host_metrics(
    overview_mock, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    operator = _user(UserRole.USER)
    _make_operator(monkeypatch, operator)
    body = await pd.get_dashboard_overview(
        time_range=MetricTimeRange.LAST_24H, current_user=operator
    )
    assert body["system_health"] == {"cpu_usage": 42.0}
    assert "user_engagement" not in body


def test_system_health_route_requires_platform_operator() -> None:
    param = inspect.signature(pd.get_system_health_metrics).parameters["current_user"]
    assert param.default.dependency is require_platform_operator


def test_tenant_admin_is_not_a_platform_operator() -> None:
    with pytest.raises(Exception) as exc:
        require_platform_operator(current_user=_user(UserRole.ADMIN))  # type: ignore[arg-type]
    assert getattr(exc.value, "status_code", None) == 403
