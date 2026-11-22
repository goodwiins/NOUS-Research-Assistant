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

import csv
import io
import uuid
from datetime import datetime, timedelta, timezone
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


# ---- export parameters are honoured or rejected (GOO-677) -------------------


def _req(**overrides: Any) -> AnalyticsExportRequest:
    end = datetime(2026, 11, 21)
    fields: dict[str, Any] = {
        "data_type": "user_behavior",
        "start_date": end - timedelta(days=7),
        "end_date": end,
        "format": "json",
    }
    return AnalyticsExportRequest(**{**fields, **overrides})


async def _export(req: AnalyticsExportRequest) -> Any:
    return await ub.export_behavior_data(
        export_request=req,
        background_tasks=BackgroundTasks(),
        current_user=_caller(),
        db=MagicMock(),
    )


@pytest.mark.parametrize("fmt", ["xml", "xlsx", ""])
async def test_export_unsupported_format_is_400(fmt: str) -> None:
    with pytest.raises(HTTPException) as ei:
        await _export(_req(format=fmt))
    assert ei.value.status_code == 400


@pytest.mark.parametrize("days", [0, -3])
async def test_export_end_not_after_start_is_400(days: int) -> None:
    end = datetime(2026, 11, 21)
    with pytest.raises(HTTPException) as ei:
        await _export(_req(start_date=end - timedelta(days=days), end_date=end))
    assert ei.value.status_code == 400


async def test_export_window_longer_than_a_year_is_400() -> None:
    end = datetime(2026, 11, 21)
    with pytest.raises(HTTPException) as ei:
        await _export(_req(start_date=end - timedelta(days=367), end_date=end))
    assert ei.value.status_code == 400


TRENDS = {
    "session_trends": [{"period": "2026-11-20", "session_count": 2}],
    "search_trends": [{"period": "2026-11-20", "search_count": 5}],
    "engagement_trends": [],
}
PATTERNS = {
    "power_users": [{"user_id": USER, "name": "A B", "session_count": 9}],
    "casual_users": [],
}


def _mock_behavior(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    trends = AsyncMock(return_value=TRENDS)
    patterns = AsyncMock(return_value=PATTERNS)
    monkeypatch.setattr(ub.user_behavior_service, "get_behavior_trends", trends)
    monkeypatch.setattr(
        ub.user_behavior_service, "identify_behavior_patterns", patterns
    )
    return trends, patterns


async def test_export_user_behavior_passes_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trends, patterns = _mock_behavior(monkeypatch)
    # An aware window is converted to the naive UTC the columns store.
    start = datetime(2026, 11, 1, 2, 0, tzinfo=timezone(timedelta(hours=2)))
    end = datetime(2026, 11, 8, 2, 0, tzinfo=timezone(timedelta(hours=2)))

    resp = await _export(_req(start_date=start, end_date=end))

    window = {"start_date": datetime(2026, 11, 1), "end_date": datetime(2026, 11, 8)}
    for mock in (trends, patterns):
        assert mock.await_args is not None
        kwargs = mock.await_args.kwargs
        assert {k: kwargs.get(k) for k in window} == window
    assert resp["data"] == {"trends": TRENDS, "patterns": PATTERNS}
    assert resp["window"] == {
        "start_date": "2026-11-01T00:00:00",
        "end_date": "2026-11-08T00:00:00",
    }


async def test_export_user_behavior_as_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_behavior(monkeypatch)

    resp = await _export(_req(format="CSV"))

    data = resp["data"]
    assert resp["format"] == "csv"
    assert set(data) == {
        "session_trends",
        "search_trends",
        "engagement_trends",
        "patterns",
    }
    sessions = list(csv.DictReader(io.StringIO(data["session_trends"])))
    assert sessions == [{"period": "2026-11-20", "session_count": "2"}]
    assert data["engagement_trends"] == ""
    patterns = list(csv.DictReader(io.StringIO(data["patterns"])))
    assert patterns == [
        {"pattern": "power_users", "user_id": USER, "name": "A B", "session_count": "9"}
    ]


# ---- CSV cells are safe to open in a spreadsheet (GOO-691) ------------------


@pytest.mark.parametrize(
    "value",
    ['=HYPERLINK("http://x")', "+1+1", "-2+3", "@SUM(A1)", "\tcmd", "\rcmd"],
)
def test_csv_prefixes_formula_cells_with_a_quote(value: str) -> None:
    # A title or name starting with = + - @ TAB CR runs as a formula in Excel,
    # LibreOffice and Sheets; the leading quote makes it literal text.
    rows = list(csv.DictReader(io.StringIO(ub._csv_table([{"title": value}]))))

    assert rows == [{"title": "'" + value}]


def test_csv_leaves_plain_text_and_numbers_alone() -> None:
    table = ub._csv_table([{"title": "a=b", "delta": -3, "rate": -0.5, "n": None}])

    rows = list(csv.DictReader(io.StringIO(table)))
    assert rows == [{"title": "a=b", "delta": "-3", "rate": "-0.5", "n": ""}]


async def test_export_user_behavior_csv_sanitizes_user_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trends = AsyncMock(return_value={"session_trends": []})
    patterns = AsyncMock(
        return_value={"power_users": [{"user_id": USER, "name": "=cmd|' /C calc'!A0"}]}
    )
    monkeypatch.setattr(ub.user_behavior_service, "get_behavior_trends", trends)
    monkeypatch.setattr(
        ub.user_behavior_service, "identify_behavior_patterns", patterns
    )

    resp = await _export(_req(format="csv"))

    rows = list(csv.DictReader(io.StringIO(resp["data"]["patterns"])))
    assert rows[0]["name"] == "'=cmd|' /C calc'!A0"


# ---- json is the export default (GOO-691) -----------------------------------


def _default_format_request() -> AnalyticsExportRequest:
    end = datetime(2026, 11, 21)
    return AnalyticsExportRequest(
        data_type="user_behavior", start_date=end - timedelta(days=7), end_date=end
    )


def test_export_request_defaults_to_json() -> None:
    assert _default_format_request().format == "json"


async def test_export_without_format_returns_structured_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_behavior(monkeypatch)

    resp = await _export(_default_format_request())

    assert resp["format"] == "json"
    assert resp["data"] == {"trends": TRENDS, "patterns": PATTERNS}
