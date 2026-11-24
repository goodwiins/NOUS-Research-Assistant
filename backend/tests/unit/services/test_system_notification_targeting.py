"""``user_notifications`` broadcasts must be addressed (GOO-740).

``broadcast_system_notification`` used to fan an untargeted notification out
to every ``user_notifications`` subscriber in every organization. The document
processing notifications (title, document id, error text) were sent that way,
so one tenant's processing events reached everyone. An untargeted notification
now goes nowhere unless the caller opts in with ``broadcast_to_all=True``
(reserved for text that carries no user or organization data), and the
document notifications are addressed to the document's uploader.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

import src.services.infrastructure.status_update_service as sus
import src.services.processing.processing_integration as pi

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _service() -> sus.StatusUpdateService:
    svc = sus.StatusUpdateService()
    svc._log_status_update = AsyncMock()  # type: ignore[method-assign]
    return svc


async def test_untargeted_notification_is_not_broadcast_to_everyone() -> None:
    svc = _service()
    with patch.object(sus, "connection_manager") as cm:
        cm.broadcast_to_channel = AsyncMock()
        cm.broadcast_to_user = AsyncMock()
        cm.broadcast_to_organization = AsyncMock()
        await svc.broadcast_system_notification(
            title="Processing Failed",
            message="Document processing failed: secret.pdf",
            notification_type="error",
        )

    cm.broadcast_to_channel.assert_not_awaited()
    cm.broadcast_to_user.assert_not_awaited()
    cm.broadcast_to_organization.assert_not_awaited()


async def test_explicit_broadcast_to_all_still_reaches_the_channel() -> None:
    svc = _service()
    with patch.object(sus, "connection_manager") as cm:
        cm.broadcast_to_channel = AsyncMock()
        await svc.broadcast_system_notification(
            title="Service Temporarily Unavailable",
            message="Please try again later.",
            notification_type="error",
            broadcast_to_all=True,
        )

    cm.broadcast_to_channel.assert_awaited_once()
    call = cm.broadcast_to_channel.await_args
    assert call is not None
    channel, message = call.args
    assert channel == sus.Channel.USER_NOTIFICATIONS.value
    assert message.target_organization is None


async def test_targeted_notification_goes_to_the_user_only() -> None:
    svc = _service()
    with patch.object(sus, "connection_manager") as cm:
        cm.broadcast_to_channel = AsyncMock()
        cm.broadcast_to_user = AsyncMock()
        await svc.broadcast_system_notification(
            title="t", message="m", target_users=["user-1"]
        )

    cm.broadcast_to_user.assert_awaited_once()
    call = cm.broadcast_to_user.await_args
    assert call is not None
    assert call.args[0] == "user-1"
    cm.broadcast_to_channel.assert_not_awaited()


async def test_error_handler_system_alerts_opt_in_to_broadcast_to_all() -> None:
    """The two generic alerts (no user or org data) keep reaching everyone."""
    from src.services.websocket import websocket_error_handler as weh

    handler = weh.WebSocketErrorHandler()
    with patch.object(weh, "status_update_service") as sus_mock:
        sus_mock.broadcast_system_notification = AsyncMock()
        ctx = AsyncMock()
        ctx.error_type = "x"
        await handler._handle_circuit_breaker_open(ctx)

    call = sus_mock.broadcast_system_notification.await_args
    assert call is not None
    assert call.kwargs.get("broadcast_to_all") is True


def _capture() -> tuple[List[Dict[str, Any]], AsyncMock]:
    calls: List[Dict[str, Any]] = []

    async def _record(**kwargs: Any) -> None:
        calls.append(kwargs)

    return calls, AsyncMock(side_effect=_record)


@pytest.mark.parametrize(
    "invoke",
    [
        lambda svc: svc._broadcast_processing_start(
            "doc-1",
            {
                "user_id": "owner-1",
                "processing_steps": [pi.ProcessingStep("s", "d", 1.0, 1.0)],
            },
        ),
        lambda svc: svc._broadcast_step_completion(
            "doc-1",
            {"completed_step": "s", "step_description": "d"},
            target_user="owner-1",
        ),
        lambda svc: svc._broadcast_processing_warning(
            "doc-1", "w", {}, target_user="owner-1"
        ),
        lambda svc: svc._broadcast_processing_error(
            "doc-1", "boom", {"user_id": "owner-1"}
        ),
        lambda svc: svc._broadcast_processing_completion(
            "doc-1", {}, target_user="owner-1"
        ),
    ],
    ids=["start", "step_completion", "warning", "error", "completion"],
)
async def test_document_notifications_target_the_uploader(invoke: Any) -> None:
    calls, record = _capture()
    svc = pi.ProcessingIntegrationService()
    with patch.object(pi, "status_update_service") as sus_mock:
        sus_mock.broadcast_system_notification = record
        sus_mock.broadcast_document_update = AsyncMock()
        await invoke(svc)

    assert len(calls) == 1
    assert calls[0].get("target_users") == ["owner-1"]
    assert not calls[0].get("broadcast_to_all")
