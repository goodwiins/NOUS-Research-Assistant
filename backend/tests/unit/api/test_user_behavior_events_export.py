"""Behavior analytics: ``POST /events`` and export (audit API2B-1, API2B-2).

API2B-1: ``track_search_event`` wrote no ``organization_id`` (NOT NULL on
``search_events``) and stored the client session token as the FK to
``search_sessions.id``, so ``POST /events`` could never succeed. It also never
checked that the session belonged to the caller.

API2B-2: the export route called the ``/content-usage`` route function without
``db``, so ``db`` was still a ``Depends`` marker and ``data_type=content_usage``
always returned 500.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.api.quality import user_behavior as ub
from src.models.quality_metrics import SearchEvent
from src.models.user import UserRole
from src.schemas.quality_metrics import AnalyticsExportRequest
from src.services.quality import user_behavior_service as svc_mod
from src.services.quality.user_behavior_service import UserBehaviorService

pytestmark = pytest.mark.unit

ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER = "11111111-1111-1111-1111-111111111111"
EVENT: dict[str, Any] = {
    "session_id": "client-token-1",
    "query": "graphene",
    "search_type": "fulltext",
    "results_count": 3,
    "response_time": 120.0,
}


def _caller(org: Any = ORG) -> Any:
    return SimpleNamespace(id=USER, role=UserRole.ADMIN, organization_id=org)


# ---- route -----------------------------------------------------------------


async def test_events_route_passes_caller_org(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    monkeypatch.setattr(ub.user_behavior_service, "track_search_event", svc)

    resp = await ub.track_search_event(event_data=dict(EVENT), current_user=_caller())

    assert resp["status"] == "tracked"
    assert svc.await_args is not None
    assert svc.await_args.kwargs["organization_id"] == ORG
    assert svc.await_args.kwargs["user_id"] == USER


async def test_events_route_unknown_session_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ub.user_behavior_service, "track_search_event", AsyncMock(return_value=None)
    )
    with pytest.raises(HTTPException) as ei:
        await ub.track_search_event(event_data=dict(EVENT), current_user=_caller())
    assert ei.value.status_code == 404


async def test_events_route_missing_field_is_422() -> None:
    with pytest.raises(HTTPException) as ei:
        await ub.track_search_event(
            event_data={"session_id": "x"}, current_user=_caller()
        )
    assert ei.value.status_code == 422


# ---- service ---------------------------------------------------------------


def _install_db(monkeypatch: pytest.MonkeyPatch, session: Any) -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = session
    monkeypatch.setattr(svc_mod, "get_db_sync", lambda: iter([db]))
    return db


async def test_service_writes_org_and_session_pk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(id=uuid.uuid4(), search_count=1, total_response_time=80.0)
    db = _install_db(monkeypatch, session)

    event = await UserBehaviorService().track_search_event(
        **EVENT, user_id=USER, organization_id=ORG
    )

    assert isinstance(event, SearchEvent)
    assert event.organization_id == uuid.UUID(ORG)
    assert event.session_id == session.id  # FK is the row pk, not the token
    db.add.assert_called_once_with(event)
    db.commit.assert_called_once()
    assert session.search_count == 2
    assert session.avg_response_time == pytest.approx(100.0)

    clauses = db.query.return_value.filter.call_args.args
    keys = {getattr(getattr(c, "left", None), "key", None) for c in clauses}
    assert keys == {"session_id", "user_id", "organization_id"}


async def test_service_foreign_session_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _install_db(monkeypatch, None)

    event = await UserBehaviorService().track_search_event(
        **EVENT, user_id=USER, organization_id=ORG
    )

    assert event is None
    db.add.assert_not_called()
    db.commit.assert_not_called()


async def test_service_without_org_does_not_open_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_db = MagicMock()
    monkeypatch.setattr(svc_mod, "get_db_sync", get_db)

    event = await UserBehaviorService().track_search_event(
        **EVENT, user_id=USER, organization_id=None
    )

    assert event is None
    get_db.assert_not_called()


# ---- export ----------------------------------------------------------------


def _export_request(data_type: str) -> AnalyticsExportRequest:
    end = datetime(2026, 11, 21)
    return AnalyticsExportRequest(
        data_type=data_type,
        start_date=end - timedelta(days=7),
        end_date=end,
        format="json",
        filters=None,
        include_metadata=True,
    )


async def test_export_content_usage_uses_the_request_session() -> None:
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        SimpleNamespace(
            document_id=uuid.uuid4(),
            title="paper",
            unique_users=2,
            search_appearances=4,
            total_clicks=1,
            avg_results_in_search=3.0,
            last_accessed=None,
        )
    ]

    resp = await ub.export_behavior_data(
        export_request=_export_request("content_usage"),
        background_tasks=BackgroundTasks(),
        current_user=_caller(),
        db=db,
    )

    rows = resp["data"]["content_usage"]
    assert [r["title"] for r in rows] == ["paper"]
    assert rows[0]["click_through_rate"] == pytest.approx(0.25)
    params = db.execute.call_args.args[1]
    assert params["org_id"] == ORG
    assert params["limit"] == 500


async def test_export_unknown_data_type_is_400() -> None:
    with pytest.raises(HTTPException) as ei:
        await ub.export_behavior_data(
            export_request=_export_request("completed"),
            background_tasks=BackgroundTasks(),
            current_user=_caller(),
            db=MagicMock(),
        )
    assert ei.value.status_code == 400
