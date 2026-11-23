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
def test_malformed_message_filter_is_rejected(raw):
    assert _validated_message_filter(raw) is None


def test_flat_message_filter_is_kept():
    raw = {"document_id": "d1", "status": ["done", "failed"], "n": None}
    assert _validated_message_filter(raw) == raw


@pytest.mark.asyncio
async def test_non_object_filter_from_status_frame_cannot_drop_org_broadcast():
    mgr = EnhancedConnectionManager()
    mgr._log_connection_event = AsyncMock()
    bad, bad_ws = _register(mgr, user_id="u1", org_id="orgA", conn_id="bad")
    _, good_ws = _register(mgr, user_id="u2", org_id="orgA", conn_id="good")

    await mgr._handle_status_update("bad", {"message_filter": "not-a-dict"})
    assert bad.message_filter is None

    await mgr.broadcast_to_organization("orgA", _msg(target_org="orgA"))

    assert len(bad_ws.sent) == 1
    assert len(good_ws.sent) == 1


@pytest.mark.asyncio
async def test_gate_error_on_one_connection_skips_only_that_connection():
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
async def test_multi_channel_broadcast_delivers_once_and_publishes_once():
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
async def test_cluster_multi_channel_message_delivers_once_per_local_connection():
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
async def test_disconnect_during_fan_out_does_not_break_delivery():
    mgr = EnhancedConnectionManager()
    _, first_ws = _register(
        mgr, user_id="u1", org_id="orgA", channels=("thread:t1",), conn_id="a"
    )
    _, second_ws = _register(
        mgr, user_id="u2", org_id="orgA", channels=("thread:t1",), conn_id="b"
    )
    original_send = first_ws.send_json

    async def send_then_unsubscribe_everyone(payload):
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
async def test_thread_event_service_uses_cluster_broadcast(monkeypatch):
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
