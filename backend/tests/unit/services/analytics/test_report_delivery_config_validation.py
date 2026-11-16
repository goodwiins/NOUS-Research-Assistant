"""A webhook delivery URL is validated when delivery_config is saved (GOO-588).

Before, the URL was only checked at delivery time, so a blocked destination
was accepted on save and the user learned of it only after the report ran.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from src.api.analytics import reports as reports_api
from src.services.analytics.report_service import ReportGenerationService
from src.utils.outbound_url_guard import UnsafeDestinationError

_PRIVATE = {"methods": ["webhook"], "webhook": {"url": "https://169.254.169.254/x"}}


def _create_kwargs(delivery_config: dict) -> dict:
    return {
        "name": "n",
        "title": "t",
        "description": None,
        "owner_id": uuid.uuid4(),
        "organization_id": None,
        "report_config": {"sections": []},
        "delivery_config": delivery_config,
    }


@pytest.mark.asyncio
async def test_create_report_rejects_non_public_webhook_before_saving() -> None:
    service = ReportGenerationService()
    with patch("src.services.analytics.report_service.get_async_session") as session:
        with pytest.raises(UnsafeDestinationError):
            await service.create_report(**_create_kwargs(_PRIVATE))
    session.assert_not_called()


@pytest.mark.asyncio
async def test_create_report_rejects_webhook_method_without_url() -> None:
    service = ReportGenerationService()
    with patch("src.services.analytics.report_service.get_async_session") as session:
        with pytest.raises(UnsafeDestinationError):
            await service.create_report(
                **_create_kwargs({"methods": ["webhook"], "webhook": {}})
            )
    session.assert_not_called()


@pytest.mark.asyncio
async def test_create_report_without_webhook_skips_url_check() -> None:
    service = ReportGenerationService()
    with (
        patch("src.services.analytics.report_service.assert_public_https_url") as guard,
        patch("src.services.analytics.report_service.get_async_session") as session,
    ):
        session.side_effect = RuntimeError("db reached")
        with pytest.raises(RuntimeError, match="db reached"):
            await service.create_report(
                **_create_kwargs({"methods": ["email"], "email": {}})
            )
    guard.assert_not_called()


@pytest.mark.asyncio
async def test_create_report_route_returns_422_for_blocked_webhook() -> None:
    user = SimpleNamespace(id=uuid.uuid4(), organization_id=None)
    with patch("src.services.analytics.report_service.get_async_session") as session:
        with pytest.raises(HTTPException) as exc_info:
            await reports_api.create_report(
                name="n",
                title="t",
                report_config={"sections": []},
                delivery_config=_PRIVATE,
                current_user=user,  # type: ignore[arg-type]
            )
    assert exc_info.value.status_code == 422
    # A fixed message: the guard's own text (which names the host) stays in logs.
    assert exc_info.value.detail == (
        "Invalid delivery_config: webhook URL must be a public HTTPS URL"
    )
    session.assert_not_called()
