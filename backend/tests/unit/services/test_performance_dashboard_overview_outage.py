"""Dashboard overview routes answer 503 for a database outage (GOO-750).

``/overview``, ``/dashboard`` and ``/export`` all read
``get_dashboard_overview``. A refused database connection used to come back
as a 500. The rule from GOO-653 / GOO-732 now applies: a connection-level
error is "unavailable" (503, fixed message) and anything else is a bug (500).

``/metrics/available`` no longer offers ``error_rate``: nothing records it,
so it has no series (GOO-732).
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, cast
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import InterfaceError, OperationalError

from src.api.quality import performance_dashboard as pd
from src.models.user import User, UserRole
from src.services.quality import performance_dashboard_service as svc_module
from src.services.quality.performance_dashboard_service import MetricTimeRange

pytestmark = pytest.mark.unit

UNAVAILABLE = "Metrics are temporarily unavailable"


def _user() -> User:
    return cast(
        User, SimpleNamespace(id=uuid4(), organization_id=uuid4(), role=UserRole.USER)
    )


async def _overview(user: User) -> Any:
    return await pd.get_dashboard_overview(
        time_range=MetricTimeRange.LAST_24H, current_user=user
    )


async def _dashboard(user: User) -> Any:
    return await pd.get_performance_dashboard(
        time_range=MetricTimeRange.LAST_24H, current_user=user
    )


async def _export(user: User) -> Any:
    return await pd.export_dashboard_data(
        format="json",
        time_range=MetricTimeRange.LAST_24H,
        metrics=[],
        current_user=user,
    )


ROUTES: Dict[str, Callable[[User], Awaitable[Any]]] = {
    "overview": _overview,
    "dashboard": _dashboard,
    "export": _export,
}


def _session_failing_with(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """The real service, with a database session that cannot be used."""

    @asynccontextmanager
    async def _session() -> AsyncIterator[Any]:
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr(svc_module, "get_async_session", _session)
    # No cache: every call must reach the database.
    monkeypatch.setattr(svc_module, "_overview_cache_key", lambda *_a, **_k: None)


CONNECTION_ERRORS = [
    pytest.param(
        OperationalError("SELECT 1", {}, Exception("connection refused")),
        id="operational",
    ),
    pytest.param(InterfaceError("SELECT 1", {}, Exception("closed")), id="interface"),
    pytest.param(ConnectionRefusedError("refused"), id="refused"),
    pytest.param(socket.gaierror("dns"), id="dns"),
    pytest.param(asyncio.TimeoutError(), id="timeout"),
]

BUGS = [
    pytest.param(RuntimeError("boom"), id="runtime"),
    pytest.param(AttributeError("no attribute"), id="attribute"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", list(ROUTES))
@pytest.mark.parametrize("error", CONNECTION_ERRORS)
async def test_database_outage_answers_503(
    monkeypatch: pytest.MonkeyPatch, route: str, error: BaseException
) -> None:
    _session_failing_with(monkeypatch, error)

    with pytest.raises(HTTPException) as raised:
        await ROUTES[route](_user())

    assert raised.value.status_code == 503
    assert raised.value.detail == UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("route", list(ROUTES))
@pytest.mark.parametrize("error", BUGS)
async def test_a_bug_still_answers_500(
    monkeypatch: pytest.MonkeyPatch, route: str, error: BaseException
) -> None:
    _session_failing_with(monkeypatch, error)

    with pytest.raises(HTTPException) as raised:
        await ROUTES[route](_user())

    assert raised.value.status_code == 500
    assert raised.value.detail == "Internal server error"


@pytest.mark.asyncio
async def test_available_metrics_do_not_offer_error_rate() -> None:
    body = await pd.get_available_metrics(current_user=_user())

    names = [
        metric["name"]
        for group in body.values()
        if isinstance(group, list)
        for metric in group
    ]
    assert names, "expected the metric groups in the response body"
    assert "error_rate" not in names
