"""GOO-726: mid-session subscribes to per-resource channels need read access.

The thread events (``thread_event_service``) publish to ``thread:<id>``,
``conversation:<id>`` and ``user:<id>:threads``. Before GOO-726 the manager's
SUBSCRIBE handler only gated the admin channels, and delivery only checks
``target_organization``. So any member of an org could subscribe to another
member's private thread and receive its events.

These tests pin the handler to the thread read rule, without a database:
``workspace_access.get_thread_lean`` / ``get_conversation`` are patched. The
PostgreSQL behaviour (real rows, real rule, real delivery) is in
``test_websocket_resource_channel_auth_postgres.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.services.websocket import websocket_manager as wm
from src.services.websocket.websocket_manager import (
    ConnectionInfo,
    EnhancedConnectionManager,
    MessageType,
)

pytestmark = pytest.mark.unit

# Fixed ids: CALLER appears in parametrize ids, which must be identical on
# every xdist worker.
CALLER = "11111111-1111-4111-8111-111111111111"
THREAD = "22222222-2222-4222-8222-222222222222"
CONVERSATION = "33333333-3333-4333-8333-333333333333"


def _manager() -> tuple[EnhancedConnectionManager, ConnectionInfo]:
    mgr = EnhancedConnectionManager()
    conn = ConnectionInfo(
        user_id=CALLER,
        organization_id="org-A",
        connection_id="c-1",
        websocket=cast(Any, object()),
        connected_at=datetime.now(timezone.utc),
        last_heartbeat=datetime.now(timezone.utc),
        subscribed_channels=set(),
        client_info={"role": "USER"},
    )
    mgr.active_connections[conn.connection_id] = conn
    mgr.subscribe_to_channel = AsyncMock()  # type: ignore[method-assign]
    mgr.send_message_to_connection = AsyncMock()  # type: ignore[method-assign]
    return mgr, conn


@pytest.fixture
def session() -> Iterator[MagicMock]:
    """Patch the manager's session factory; yields the fake session."""
    db = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    with patch.object(wm, "get_async_session", return_value=cm):
        yield db


async def _subscribe(mgr: EnhancedConnectionManager, channel: str) -> None:
    await mgr.handle_client_message(
        "c-1", json.dumps({"type": "subscribe", "data": {"channel": channel}})
    )


def _denial(mgr: EnhancedConnectionManager) -> Any:
    mgr.subscribe_to_channel.assert_not_awaited()  # type: ignore[attr-defined]
    sent = mgr.send_message_to_connection.await_args.args[1]  # type: ignore[attr-defined]
    assert sent.type == MessageType.ERROR
    assert sent.data["error"] == "subscription_denied"
    return sent.data


@pytest.mark.asyncio
async def test_thread_subscribe_is_refused_without_read_access(
    session: MagicMock,
) -> None:
    mgr, _ = _manager()
    lean = AsyncMock(return_value=None)
    with patch("src.services.threads.workspace_access.get_thread_lean", lean):
        await _subscribe(mgr, f"thread:{THREAD}")
    _denial(mgr)
    # The read rule ran for THIS caller and THIS thread.
    assert lean.await_args is not None
    _, thread_id, user_id = lean.await_args.args
    assert (str(thread_id), str(user_id)) == (THREAD, CALLER)


@pytest.mark.asyncio
async def test_thread_subscribe_is_allowed_with_read_access(
    session: MagicMock,
) -> None:
    mgr, _ = _manager()
    with patch(
        "src.services.threads.workspace_access.get_thread_lean",
        AsyncMock(return_value=object()),
    ):
        await _subscribe(mgr, f"thread:{THREAD}")
    mgr.subscribe_to_channel.assert_awaited_once_with("c-1", f"thread:{THREAD}")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_conversation_subscribe_uses_the_conversation_read_rule(
    session: MagicMock,
) -> None:
    mgr, _ = _manager()
    get_conversation = AsyncMock(return_value=None)
    with patch(
        "src.services.threads.workspace_access.get_conversation", get_conversation
    ):
        await _subscribe(mgr, f"conversation:{CONVERSATION}")
    _denial(mgr)
    assert get_conversation.await_args is not None
    _, conversation_id, user_id = get_conversation.await_args.args
    assert (str(conversation_id), str(user_id)) == (CONVERSATION, CALLER)

    mgr, _ = _manager()
    with patch(
        "src.services.threads.workspace_access.get_conversation",
        AsyncMock(return_value=object()),
    ):
        await _subscribe(mgr, f"conversation:{CONVERSATION}")
    mgr.subscribe_to_channel.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_user_threads_channel_is_only_the_callers_own() -> None:
    mgr, _ = _manager()
    await _subscribe(mgr, f"user:{uuid4()}:threads")
    _denial(mgr)

    mgr, _ = _manager()
    await _subscribe(mgr, f"user:{CALLER}:threads")
    mgr.subscribe_to_channel.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel",
    [
        "thread:not-a-uuid",
        "thread:",
        "conversation:../x",
        f"user:{CALLER}",
        f"user:{CALLER}:other",
        f"user:{CALLER}:threads:x",
    ],
)
async def test_malformed_resource_channels_are_refused(
    session: MagicMock, channel: str
) -> None:
    mgr, _ = _manager()
    await _subscribe(mgr, channel)
    _denial(mgr)


@pytest.mark.asyncio
async def test_denial_does_not_say_whether_the_thread_exists(
    session: MagicMock,
) -> None:
    """Forbidden, missing and malformed get the same frame (bar the echo)."""
    frames = []
    for channel in (f"thread:{THREAD}", "thread:not-a-uuid"):
        mgr, _ = _manager()
        with patch(
            "src.services.threads.workspace_access.get_thread_lean",
            AsyncMock(return_value=None),
        ):
            await _subscribe(mgr, channel)
        data = dict(_denial(mgr))
        assert data.pop("channel") == channel
        frames.append(data)
    assert frames[0] == frames[1]
    assert set(frames[0]) == {"error", "reason"}


@pytest.mark.asyncio
async def test_a_failing_access_check_refuses(session: MagicMock) -> None:
    mgr, _ = _manager()
    with patch(
        "src.services.threads.workspace_access.get_thread_lean",
        AsyncMock(side_effect=RuntimeError("db down")),
    ):
        await _subscribe(mgr, f"thread:{THREAD}")
    _denial(mgr)


@pytest.mark.asyncio
async def test_flat_channels_keep_their_existing_gate() -> None:
    """No DB check for the flat channels; ``document_processing`` still works."""
    mgr, _ = _manager()
    with patch.object(wm, "get_async_session") as factory:
        await _subscribe(mgr, "document_processing")
    factory.assert_not_called()
    mgr.subscribe_to_channel.assert_awaited_once()  # type: ignore[attr-defined]
