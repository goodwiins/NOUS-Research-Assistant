"""One bad client frame or one bad connection must not drop a broadcast for
everyone else, and thread events must fan out safely across workers (GOO-652).

* PL-1: a client could set ``message_filter`` to a non-object (e.g. a string)
  via a status frame. ``should_receive_message`` then raised inside
  ``_deliver_local`` and the whole broadcast was lost for every connection in
  that org/channel.
* IS2C-1: ``ThreadEventService`` iterated live subscriber sets across awaits,
  sent sequentially, and never published to Redis, so clients on other
  workers/replicas missed thread events.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.services.threads.thread_event_service import ThreadEventService
from src.services.websocket.websocket_manager import (
    EnhancedConnectionManager,
    _validated_message_filter,
)
from tests.unit.services.test_websocket_fanout import _msg, _register

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "raw",
    [
        "doc-1",
        ["doc-1"],
        42,
        {"document_id": {"nested": "object"}},
        {"document_id": [{"nested": "object"}]},
        {1: "non-string key"},
    ],
)
def test_malformed_message_filter_is_rejected(raw: object) -> None:
    assert _validated_message_filter(raw) is None


def test_flat_message_filter_is_kept() -> None:
    raw = {"document_id": "d1", "status": ["done", "failed"], "n": None}
    assert _validated_message_filter(raw) == raw


@pytest.mark.asyncio
async def test_non_object_filter_from_status_frame_cannot_drop_org_broadcast() -> None:
    mgr = EnhancedConnectionManager()
    mgr._log_connection_event = AsyncMock()  # type: ignore[method-assign]
    bad, bad_ws = _register(mgr, user_id="u1", org_id="orgA", conn_id="bad")
    _, good_ws = _register(mgr, user_id="u2", org_id="orgA", conn_id="good")

    await mgr._handle_status_update("bad", {"message_filter": "not-a-dict"})
    assert bad.message_filter is None

    await mgr.broadcast_to_organization("orgA", _msg(target_org="orgA"))

    assert len(bad_ws.sent) == 1
    assert len(good_ws.sent) == 1


@pytest.mark.asyncio
async def test_gate_error_on_one_connection_skips_only_that_connection() -> None:
    mgr = EnhancedConnectionManager()
    broken, broken_ws = _register(mgr, user_id="u1", org_id="orgA", conn_id="b")
    _, good_ws = _register(mgr, user_id="u2", org_id="orgA", conn_id="g")
    # Bypass validation to simulate any other gate failure.
    broken.message_filter = "not-a-dict-but-truthy"
    broken.should_receive_message = lambda _m: (_ for _ in ()).throw(  # type: ignore[method-assign]
        TypeError("boom")
    )

    await mgr.broadcast_to_organization("orgA", _msg(target_org="orgA"))

    assert broken_ws.sent == []  # fails closed for the broken connection
    assert len(good_ws.sent) == 1


@pytest.mark.asyncio
async def test_multi_channel_broadcast_delivers_once_and_publishes_once() -> None:
    mgr = EnhancedConnectionManager()
    mgr.redis_client = AsyncMock()
    _, both_ws = _register(
        mgr,
        user_id="u1",
        org_id="orgA",
        channels=("thread:t1", "conversation:c1"),
        conn_id="both",
    )
    _, one_ws = _register(
        mgr, user_id="u2", org_id="orgA", channels=("conversation:c1",), conn_id="one"
    )
    _, other_org_ws = _register(
        mgr, user_id="u3", org_id="orgB", channels=("thread:t1",), conn_id="x"
    )

    await mgr.broadcast_to_channels(
        ["thread:t1", "conversation:c1"], _msg(target_org="orgA")
    )

    assert len(both_ws.sent) == 1  # de-duplicated across channels
    assert len(one_ws.sent) == 1
    assert other_org_ws.sent == []  # tenant gate still applies
    mgr.redis_client.publish.assert_awaited_once()
    payload = json.loads(mgr.redis_client.publish.await_args.args[1])
    assert payload["kind"] == "channels"
    assert payload["targets"] == ["thread:t1", "conversation:c1"]
    assert payload["channel"] == "thread:t1"  # legacy workers still route


@pytest.mark.asyncio
async def test_cluster_multi_channel_message_delivers_once_per_local_connection() -> (
    None
):
    sender = EnhancedConnectionManager()
    sender.redis_client = AsyncMock()
    await sender.broadcast_to_channels(
        ["thread:t1", "conversation:c1"], _msg(target_org="orgA")
    )
    payload = json.loads(sender.redis_client.publish.await_args.args[1])

    receiver = EnhancedConnectionManager()
    receiver.redis_client = AsyncMock()
    _, ws = _register(
        receiver,
        user_id="u1",
        org_id="orgA",
        channels=("thread:t1", "conversation:c1"),
    )

    await receiver._dispatch_cluster_message(payload)

    assert len(ws.sent) == 1
    receiver.redis_client.publish.assert_not_awaited()  # never re-published


@pytest.mark.asyncio
async def test_disconnect_during_fan_out_does_not_break_delivery() -> None:
    mgr = EnhancedConnectionManager()
    _, first_ws = _register(
        mgr, user_id="u1", org_id="orgA", channels=("thread:t1",), conn_id="a"
    )
    _, second_ws = _register(
        mgr, user_id="u2", org_id="orgA", channels=("thread:t1",), conn_id="b"
    )
    original_send = first_ws.send_json

    async def send_then_unsubscribe_everyone(payload: Any) -> None:
        await original_send(payload)
        await asyncio.sleep(0)
        mgr.channel_subscribers["thread:t1"].clear()

    first_ws.send_json = send_then_unsubscribe_everyone

    service = ThreadEventService()
    service._manager = mgr
    await service._broadcast_to_channels(["thread:t1"], _msg("orgA"))

    assert len(first_ws.sent) == 1
    assert len(second_ws.sent) == 1


@pytest.mark.asyncio
async def test_thread_event_service_uses_cluster_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = EnhancedConnectionManager()
    mgr.redis_client = AsyncMock()
    service = ThreadEventService()
    monkeypatch.setattr(service, "_manager", mgr)

    await service.broadcast_message_created(
        message_id="m1",
        thread_id="t1",
        conversation_id="c1",
        user_id="u1",
        organization_id="orgA",
        role="user",
        content_preview="hello",
    )

    mgr.redis_client.publish.assert_awaited_once()
    payload = json.loads(mgr.redis_client.publish.await_args.args[1])
    assert payload["targets"] == ["thread:t1", "conversation:c1"]
    assert payload["message"]["target_organization"] == "orgA"


class _HangingRedis:
    """A black-holed Redis: publish never returns."""

    def __init__(self) -> None:
        self.calls = 0

    async def publish(self, *_args: Any) -> None:
        self.calls += 1
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_hanging_redis_publish_is_bounded_and_then_skipped() -> None:
    mgr = EnhancedConnectionManager()
    hanging = _HangingRedis()
    mgr.redis_client = hanging  # type: ignore[assignment]
    _, ws = _register(
        mgr, user_id="u1", org_id="orgA", channels=("thread:t1",), conn_id="a"
    )

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(
        mgr.broadcast_to_channels(["thread:t1"], _msg(target_org="orgA")), timeout=3
    )
    # Bounded by the publish timeout, not by Redis.
    assert loop.time() - started < 2
    assert len(ws.sent) == 1  # local delivery is unaffected

    # Inside the back-off window the next broadcast does not touch Redis.
    await mgr.broadcast_to_channels(["thread:t1"], _msg(target_org="orgA"))
    assert hanging.calls == 1
    assert len(ws.sent) == 2


@pytest.mark.asyncio
async def test_raising_redis_publish_is_not_fatal() -> None:
    mgr = EnhancedConnectionManager()
    mgr.redis_client = AsyncMock()
    mgr.redis_client.publish.side_effect = ConnectionError("redis down")
    _, ws = _register(
        mgr, user_id="u1", org_id="orgA", channels=("thread:t1",), conn_id="a"
    )

    await mgr.broadcast_to_channels(["thread:t1"], _msg(target_org="orgA"))

    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_thread_event_broadcast_failure_does_not_raise() -> None:
    service = ThreadEventService()
    broken = AsyncMock()
    broken.broadcast_to_channels.side_effect = RuntimeError("fan-out exploded")
    service._manager = broken

    await service.broadcast_thread_deleted(
        thread_id="t1", conversation_id="c1", user_id="u1", organization_id="orgA"
    )

    broken.broadcast_to_channels.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_thread_route_returns_when_broadcast_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.threads import threads as routes

    chat = AsyncMock()
    chat.get_thread.return_value = SimpleNamespace(conversation_id=uuid.uuid4())
    chat.delete_thread.return_value = True
    monkeypatch.setattr(routes, "get_chat_service", lambda _db: chat)
    broken = AsyncMock()
    broken.broadcast_to_channels.side_effect = ConnectionError("redis down")
    monkeypatch.setattr(routes.thread_event_service, "_manager", broken)
    user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())

    result = await routes.delete_thread(
        thread_id=uuid.uuid4(), current_user=user, db=AsyncMock()  # type: ignore[arg-type]
    )

    assert result is None
    chat.delete_thread.assert_awaited_once()
    broken.broadcast_to_channels.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_client_is_built_with_short_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.websocket import websocket_manager as wm

    seen: dict[str, Any] = {}

    def fake_from_url(_url: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise ConnectionError("stop here")

    monkeypatch.setattr(wm.redis, "from_url", fake_from_url)
    mgr = EnhancedConnectionManager()
    await mgr.initialize()

    assert 0 < seen["socket_connect_timeout"] <= 1
    # Longer than the listener's 1s get_message poll, still short.
    assert 1 < seen["socket_timeout"] <= 5
    assert mgr.redis_client is None  # falls back to single-instance mode
