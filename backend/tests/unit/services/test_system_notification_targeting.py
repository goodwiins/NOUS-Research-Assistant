"""``user_notifications`` broadcasts must be addressed (GOO-740).

``broadcast_system_notification`` used to fan an untargeted notification out
to every ``user_notifications`` subscriber in every organization. The document
processing notifications (title, document id, error text) were sent that way,
so one tenant's processing events reached everyone. An untargeted notification
now goes nowhere unless the caller opts in with ``broadcast_to_all=True``
(reserved for text that carries no user or organization data), and the
document notifications are addressed to the document's uploader, or to the
document's organization when it has no uploader.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

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


_OWNER_CTX: Dict[str, Any] = {"user_id": "owner-1", "organization_id": "org-1"}
_ORG_ONLY_CTX: Dict[str, Any] = {"user_id": None, "organization_id": "org-1"}
_NO_TARGET_CTX: Dict[str, Any] = {"user_id": None, "organization_id": None}


def _invokers(ctx: Dict[str, Any] | None) -> List[Any]:
    """One call per document notification, each given ``ctx``."""
    start_ctx = {
        **(ctx or {}),
        "processing_steps": [pi.ProcessingStep("s", "d", 1.0, 1.0)],
    }
    return [
        lambda svc: svc._broadcast_processing_start("doc-1", start_ctx),
        lambda svc: svc._broadcast_step_completion(
            "doc-1", {"completed_step": "s", "step_description": "d"}, ctx
        ),
        lambda svc: svc._broadcast_processing_warning("doc-1", "w", {}, ctx),
        lambda svc: svc._broadcast_processing_error("doc-1", "boom", ctx),
        lambda svc: svc._broadcast_processing_completion("doc-1", {}, ctx),
    ]


_IDS = ["start", "step_completion", "warning", "error", "completion"]


async def _notifications(invoke: Any) -> List[Dict[str, Any]]:
    calls, record = _capture()
    svc = pi.ProcessingIntegrationService()
    with patch.object(pi, "status_update_service") as sus_mock:
        sus_mock.broadcast_system_notification = record
        sus_mock.broadcast_document_update = AsyncMock()
        await invoke(svc)
    assert len(calls) == 1
    return calls


@pytest.mark.parametrize("index", range(5), ids=_IDS)
async def test_document_notifications_target_the_uploader_only(index: int) -> None:
    calls = await _notifications(_invokers(_OWNER_CTX)[index])

    assert calls[0].get("target_users") == ["owner-1"]
    # The organization is a fallback, not an extra audience.
    assert not calls[0].get("target_organizations")
    assert not calls[0].get("broadcast_to_all")


@pytest.mark.parametrize("index", range(5), ids=_IDS)
async def test_document_without_uploader_notifies_its_organization(
    index: int,
) -> None:
    """Uploader deleted or system ingest: the document's org is told."""
    calls = await _notifications(_invokers(_ORG_ONLY_CTX)[index])

    assert calls[0].get("target_organizations") == ["org-1"]
    assert not calls[0].get("target_users")
    assert not calls[0].get("broadcast_to_all")


@pytest.mark.parametrize("index", range(5), ids=_IDS)
async def test_document_without_uploader_or_org_stays_untargeted(
    index: int,
) -> None:
    calls = await _notifications(_invokers(_NO_TARGET_CTX)[index])

    assert not calls[0].get("target_users")
    assert not calls[0].get("target_organizations")
    assert not calls[0].get("broadcast_to_all")


async def test_error_without_context_stays_untargeted() -> None:
    """Document not found: no context, so nobody can be addressed."""
    calls = await _notifications(
        lambda svc: svc._broadcast_processing_error("doc-1", "boom", None)
    )

    assert not calls[0].get("target_users")
    assert not calls[0].get("target_organizations")
    assert not calls[0].get("broadcast_to_all")


async def test_org_fallback_reaches_only_that_organization() -> None:
    """End to end through the status service: org delivery, no channel fan-out."""
    svc = _service()
    with patch.object(sus, "connection_manager") as cm:
        cm.broadcast_to_channel = AsyncMock()
        cm.broadcast_to_user = AsyncMock()
        cm.broadcast_to_organization = AsyncMock()
        await svc.broadcast_system_notification(
            title="Processing Failed",
            message="m",
            notification_type="error",
            **pi._notification_targets(_ORG_ONLY_CTX),
        )

    cm.broadcast_to_organization.assert_awaited_once()
    call = cm.broadcast_to_organization.await_args
    assert call is not None
    assert call.args[0] == "org-1"
    cm.broadcast_to_user.assert_not_awaited()
    cm.broadcast_to_channel.assert_not_awaited()


@pytest.mark.parametrize(
    ("uploader", "expected"),
    [
        (None, {"target_users": None, "target_organizations": ["org-1"]}),
        ("owner-1", {"target_users": ["owner-1"], "target_organizations": None}),
    ],
    ids=["no_uploader", "uploader"],
)
async def test_public_entry_points_pass_the_document_targets(
    uploader: str | None, expected: Dict[str, Any]
) -> None:
    """complete_step, add_processing_warning and complete_processing route too."""
    document = SimpleNamespace(uploaded_by_user_id=uploader, organization_id="org-1")
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=document))
    )
    session.commit = AsyncMock()

    @asynccontextmanager
    async def _session() -> AsyncIterator[Any]:
        yield session

    svc = pi.ProcessingIntegrationService()
    svc._update_document_status = AsyncMock()  # type: ignore[method-assign]
    step = pi.ProcessingStep("s", "d", 1.0, 1.0)
    svc._active_jobs["job-1"] = {
        "document_id": "doc-1",
        "job_id": None,
        "organization_id": "org-1",
        "user_id": uploader,
        "processing_steps": [step],
        "total_weight": 1.0,
        "completed_weight": 0.0,
        "current_step_index": 0,
        "step_times": {},
        "warnings": [],
    }

    calls, record = _capture()
    with (
        patch.object(pi, "status_update_service") as sus_mock,
        patch.object(pi, "get_async_session", _session),
    ):
        sus_mock.broadcast_system_notification = record
        sus_mock.broadcast_document_update = AsyncMock()
        await svc.complete_step("job-1", "s")
        await svc.add_processing_warning("job-1", "w")
        await svc.complete_processing("doc-1")

    assert len(calls) == 3
    for call in calls:
        assert {
            "target_users": call.get("target_users"),
            "target_organizations": call.get("target_organizations"),
        } == expected
        assert not call.get("broadcast_to_all")
