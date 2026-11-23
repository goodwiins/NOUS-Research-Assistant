"""GOO-726 on PostgreSQL: thread channel subscribes follow the thread read rule.

Real rows, the real ``workspace_access`` rule, a real
``EnhancedConnectionManager`` and the real ``ThreadEventService`` fan-out. Only
the sockets are fakes that record what they are sent.

Two members of one org: ``owner`` has a private workspace and a shared
workspace (``peer`` is a ``WorkspaceMember`` of it); ``outsider`` is in
another org. ``peer`` is refused ``owner``'s private thread and receives none
of its events; the shared thread works; ``outsider`` is refused.

Each test owns a generated schema on ``ORCHESTRATION_TEST_DATABASE_URL`` (the
CI PostgreSQL lane sets it; there ``REQUIRE_POSTGRES_TESTS=1`` turns a skip
into a failure) and drops it after.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Column, MetaData, Table, insert, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.models.base import Base
from src.models.conversation import Conversation
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.services.threads.thread_event_service import ThreadEventService
from src.services.websocket import websocket_manager as wm
from src.services.websocket.websocket_manager import (
    ConnectionInfo,
    EnhancedConnectionManager,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

_MODELS: list[Any] = [Organization, Workspace, WorkspaceMember, Conversation, Thread]


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    url = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    assert url is not None
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError("PostgreSQL is required")
    schema = "goo726_" + uuid4().hex
    admin = create_async_engine(url)
    scoped = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        yield scoped
    finally:
        await scoped.dispose()
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await admin.dispose()


@dataclass
class Socket:
    sent: list[dict] = field(default_factory=list)

    async def send_json(self, payload: dict) -> None:
        self.sent.append(json.loads(json.dumps(payload, default=str)))


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    org_a: UUID
    org_b: UUID
    owner: UUID
    peer: UUID
    outsider: UUID
    private_conversation: UUID
    private_thread: UUID
    shared_conversation: UUID
    shared_thread: UUID


async def _create_tables(engine: AsyncEngine) -> None:
    names = {model.__tablename__ for model in _MODELS}
    stubs = MetaData()
    for model in _MODELS:
        for fk in model.__table__.foreign_keys:
            target = fk.column
            if target.table.name not in names and target.table.name not in stubs.tables:
                Table(
                    target.table.name,
                    stubs,
                    Column(target.name, target.type, primary_key=True),
                )
    async with engine.begin() as conn:
        await conn.run_sync(stubs.create_all)
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[model.__table__ for model in _MODELS]
            )
        )


@pytest.fixture
async def world(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[World]:
    await _create_tables(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    w = World(factory, *(uuid4() for _ in range(9)))
    private_ws, shared_ws = uuid4(), uuid4()
    async with factory() as db:
        for org in (w.org_a, w.org_b):
            await db.execute(
                insert(Organization).values(
                    id=org, name=str(org), storage_limit_bytes=1000000
                )
            )
        # ``users`` is a one-column stub (FK target only).
        for user in (w.owner, w.peer, w.outsider):
            await db.execute(text("INSERT INTO users (id) VALUES (:id)"), {"id": user})
        for ws, name in ((private_ws, "private"), (shared_ws, "shared")):
            await db.execute(
                insert(Workspace).values(
                    id=ws,
                    name=name,
                    owner_id=w.owner,
                    organization_id=w.org_a,
                    is_public=False,
                )
            )
        await db.execute(
            insert(WorkspaceMember).values(
                id=uuid4(),
                workspace_id=shared_ws,
                user_id=w.peer,
                role=WorkspaceRole.VIEWER,
            )
        )
        for ws, conversation, thread in (
            (private_ws, w.private_conversation, w.private_thread),
            (shared_ws, w.shared_conversation, w.shared_thread),
        ):
            await db.execute(
                insert(Conversation).values(
                    id=conversation,
                    workspace_id=ws,
                    title="c",
                    created_by_id=w.owner,
                )
            )
            await db.execute(
                insert(Thread).values(
                    id=thread, conversation_id=conversation, title="t"
                )
            )
        await db.commit()

    @asynccontextmanager
    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            yield db

    monkeypatch.setattr(wm, "get_async_session", session)
    yield w


def _connect(
    mgr: EnhancedConnectionManager, user: UUID, org: UUID
) -> tuple[str, Socket]:
    socket = Socket()
    connection_id = f"conn-{user}"
    info = ConnectionInfo(
        user_id=str(user),
        organization_id=str(org),
        connection_id=connection_id,
        websocket=cast(Any, socket),
        connected_at=datetime.now(timezone.utc),
        last_heartbeat=datetime.now(timezone.utc),
        subscribed_channels=set(),
        client_info={"role": "USER"},
    )
    mgr.active_connections[connection_id] = info
    mgr.user_connections[str(user)].add(connection_id)
    mgr.organization_connections[str(org)].add(connection_id)
    return connection_id, socket


async def _subscribe(mgr: EnhancedConnectionManager, conn: str, channel: str) -> None:
    await mgr.handle_client_message(
        conn, json.dumps({"type": "subscribe", "data": {"channel": channel}})
    )


def _frames(socket: Socket, kind: str) -> list[dict]:
    return [frame for frame in socket.sent if frame.get("type") == kind]


async def _thread_updated(
    mgr: EnhancedConnectionManager, w: World, thread: UUID, conversation: UUID
) -> None:
    events = ThreadEventService()
    events._manager = mgr
    await events.broadcast_thread_updated(
        thread_id=str(thread),
        conversation_id=str(conversation),
        user_id=str(w.owner),
        organization_id=str(w.org_a),
        changes={"title": "secret title"},
    )


async def test_same_org_member_is_refused_a_private_thread_and_receives_nothing(
    world: World,
) -> None:
    mgr = EnhancedConnectionManager()
    owner_conn, owner_socket = _connect(mgr, world.owner, world.org_a)
    peer_conn, peer_socket = _connect(mgr, world.peer, world.org_a)

    await _subscribe(mgr, owner_conn, f"thread:{world.private_thread}")
    for channel in (
        f"thread:{world.private_thread}",
        f"conversation:{world.private_conversation}",
        f"user:{world.owner}:threads",
    ):
        await _subscribe(mgr, peer_conn, channel)

    denials = _frames(peer_socket, "error")
    assert [d["data"]["channel"] for d in denials] == [
        f"thread:{world.private_thread}",
        f"conversation:{world.private_conversation}",
        f"user:{world.owner}:threads",
    ]
    assert all(d["data"]["error"] == "subscription_denied" for d in denials)
    assert mgr.active_connections[peer_conn].subscribed_channels == set()

    await _thread_updated(mgr, world, world.private_thread, world.private_conversation)
    assert len(_frames(owner_socket, "thread_updated")) == 1
    assert _frames(peer_socket, "thread_updated") == []
    assert "secret title" not in json.dumps(peer_socket.sent)


async def test_a_shared_thread_can_be_subscribed_and_delivers(world: World) -> None:
    mgr = EnhancedConnectionManager()
    peer_conn, peer_socket = _connect(mgr, world.peer, world.org_a)

    await _subscribe(mgr, peer_conn, f"thread:{world.shared_thread}")
    await _subscribe(mgr, peer_conn, f"conversation:{world.shared_conversation}")
    await _subscribe(mgr, peer_conn, f"user:{world.peer}:threads")

    assert _frames(peer_socket, "error") == []
    assert mgr.active_connections[peer_conn].subscribed_channels == {
        f"thread:{world.shared_thread}",
        f"conversation:{world.shared_conversation}",
        f"user:{world.peer}:threads",
    }
    await _thread_updated(mgr, world, world.shared_thread, world.shared_conversation)
    # One delivery, though two of its channels match (de-duplicated).
    assert len(_frames(peer_socket, "thread_updated")) == 1


async def test_cross_org_user_is_refused_and_receives_nothing(world: World) -> None:
    mgr = EnhancedConnectionManager()
    outsider_conn, outsider_socket = _connect(mgr, world.outsider, world.org_b)

    for channel in (
        f"thread:{world.private_thread}",
        f"thread:{world.shared_thread}",
        f"conversation:{world.shared_conversation}",
    ):
        await _subscribe(mgr, outsider_conn, channel)

    assert len(_frames(outsider_socket, "error")) == 3
    assert mgr.active_connections[outsider_conn].subscribed_channels == set()
    await _thread_updated(mgr, world, world.shared_thread, world.shared_conversation)
    assert _frames(outsider_socket, "thread_updated") == []


async def test_refusal_is_the_same_for_a_thread_that_does_not_exist(
    world: World,
) -> None:
    mgr = EnhancedConnectionManager()
    peer_conn, peer_socket = _connect(mgr, world.peer, world.org_a)
    await _subscribe(mgr, peer_conn, f"thread:{world.private_thread}")
    await _subscribe(mgr, peer_conn, f"thread:{uuid4()}")
    forbidden, missing = (
        {k: v for k, v in frame["data"].items() if k != "channel"}
        for frame in _frames(peer_socket, "error")
    )
    assert forbidden == missing
