"""``/dashboard`` reports real values or null, never made-up defaults.

GOO-724: ``get_performance_dashboard`` read ``total_searches``, ``uptime``,
``active_users`` and friends from the top level of the overview, where they
never exist (the overview nests them in sections), so every value was a
default: zeros and ``"99.9%"`` uptime. It now flattens the real sections,
respecting the per-role scoping of ``_scope_overview_for`` (GOO-633).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from src.api.quality import performance_dashboard as pd
from src.core import dependencies
from src.models.user import User, UserRole
from src.services.quality.performance_dashboard_service import MetricTimeRange

pytestmark = pytest.mark.unit


def _overview() -> Dict[str, Any]:
    # Shape of PerformanceDashboardService.get_dashboard_overview.
    return {
        "timestamp": "2026-11-23T00:00:00",
        "time_range": "24h",
        "system_health": {"error_rate": 0.02, "uptime": 86400.0},
        "search_performance": {"total_searches": 42, "avg_response_time": 310.5},
        "quality_metrics": {"overall_score": 0.9},
        "user_engagement": {"active_users": 7, "top_users": []},
        "alerts": [{"id": "a1"}],
    }


def _user(role: UserRole) -> User:
    return cast(User, SimpleNamespace(id=uuid4(), organization_id=uuid4(), role=role))


async def _dashboard(overview: Dict[str, Any], user: User) -> Dict[str, Any]:
    with patch.object(
        pd.performance_dashboard_service,
        "get_dashboard_overview",
        new=AsyncMock(return_value=overview),
    ):
        return cast(
            Dict[str, Any],
            await pd.get_performance_dashboard(
                time_range=MetricTimeRange.LAST_24H, current_user=user
            ),
        )


@pytest.mark.asyncio
async def test_member_gets_search_values_and_nulls_for_hidden_sections() -> None:
    body = await _dashboard(_overview(), _user(UserRole.USER))
    assert body["system_metrics"] == {
        "total_documents": None,
        "total_searches": 42,
        "avg_response_time": 310.5,
        "uptime": None,
        "error_rate": None,
        "active_users": None,
    }
    assert body["alerts"] == [{"id": "a1"}]
    assert "99.9" not in repr(body)


@pytest.mark.asyncio
async def test_org_admin_gets_active_users_but_not_host_error_rate() -> None:
    body = await _dashboard(_overview(), _user(UserRole.ADMIN))
    assert body["system_metrics"]["active_users"] == 7
    assert body["system_metrics"]["error_rate"] is None


@pytest.mark.asyncio
async def test_platform_operator_gets_error_rate_but_never_host_uptime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user(UserRole.USER)
    monkeypatch.setattr(
        dependencies.settings, "PLATFORM_OPERATOR_USER_IDS", str(user.id)
    )
    body = await _dashboard(_overview(), user)
    assert body["system_metrics"]["error_rate"] == 0.02
    # Host uptime in seconds is not an availability figure.
    assert body["system_metrics"]["uptime"] is None


@pytest.mark.asyncio
async def test_empty_overview_is_all_null_not_defaults() -> None:
    body = await _dashboard({}, _user(UserRole.ADMIN))
    assert set(body["system_metrics"].values()) == {None}
