"""GOO-761: a top user with no timed search reports a null average response
time, not 0 ms."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional
from uuid import uuid4

import pytest

from src.services.quality.performance_dashboard_service import (
    MetricTimeRange,
    PerformanceDashboardService,
)

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, rows: List[Any]) -> None:
        self.rows = rows

    def fetchall(self) -> List[Any]:
        return self.rows

    def fetchone(self) -> Optional[Any]:
        return self.rows[0] if self.rows else None


class _Db:
    """Answers the engagement query, then the top-users query."""

    def __init__(self, *results: List[Any]) -> None:
        self.results = list(results)

    async def execute(self, *_args: Any, **_kwargs: Any) -> _Result:
        return _Result(self.results.pop(0))


def _user(avg: Optional[float]) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        first_name="Ada",
        last_name="L",
        session_count=1,
        search_count=1,
        avg_response_time=avg,
    )


@pytest.mark.asyncio
async def test_a_user_without_timed_searches_has_no_average(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(PerformanceDashboardService)

    async def trend(*_args: Any) -> str:
        return "stable"

    monkeypatch.setattr(service, "_calculate_engagement_trend", trend)
    engagement = SimpleNamespace(
        active_users=2, total_sessions=2, avg_duration=0, total_searches=2
    )
    db = _Db([engagement], [_user(None), _user(250.0)])

    metrics = await service.get_user_engagement_metrics(
        str(uuid4()), MetricTimeRange.LAST_7D, db=db  # type: ignore[arg-type]
    )

    assert [u["avg_response_time"] for u in metrics.top_users] == [None, 250.0]
