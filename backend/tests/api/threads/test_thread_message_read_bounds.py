"""Bounded message reads for thread context and ``GET /threads/{id}`` (GOO-712).

``ChatService.get_thread_context`` reads only the newest ``max_messages`` live
rows and the thread-detail route only the newest
``THREAD_DETAIL_MAX_MESSAGES``, both through a ``LIMIT``-ed SQL page instead
of the whole ``Thread.messages`` relationship. For small threads both return
what the old full load returned. On a branched thread both read the newest
rows of the selected path, and older ones page with the head pinned.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.api.threads import threads as threads_routes
from src.core.config import settings
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.chat_message import ChatMessage, MessageRole
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.thread import Thread
from src.models.user import User
from src.services.threads import message_service, thread_service
from src.services.threads.chat_service import ChatService
from src.services.threads.message_branches import BranchAccessError
from src.services.threads.thread_service import THREAD_DETAIL_MAX_MESSAGES

ThreadFactory = Callable[..., Awaitable[Thread]]
UserFactory = Callable[..., Awaitable[User]]


async def _seed(
    db: AsyncSession,
    thread: Thread,
    user: User,
    token_counts: list[int],
) -> list[ChatMessage]:
    """Insert one message per token count, oldest first, in a single commit."""
    start = datetime.utcnow() - timedelta(hours=1)
    rows = []
    for index, tokens in enumerate(token_counts):
        created = start + timedelta(milliseconds=index * 10)
        rows.append(
            ChatMessage(
                thread_id=thread.id,
                user_id=user.id,
                role=MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT,
                content=f"msg-{index}",
                token_count=tokens,
                created_at=created,
                updated_at=created,
            )
        )
    db.add_all(rows)
    await db.commit()
    db.info["_created"]["chat_messages"].extend(row.id for row in rows)
    return rows


async def _small_thread(
    db: AsyncSession, thread: Thread, user: User
) -> list[ChatMessage]:
    """Five messages: index 1 soft-deleted, index 3 superseded by index 4."""
    rows = await _seed(db, thread, user, [100, 200, 300, 400, 500])
    rows[1].is_deleted = True
    rows[3].superseded_by_message_id = rows[4].id
    await db.commit()
    return rows


async def _branched_thread(
    db: AsyncSession, thread: Thread, user: User, path_length: int
) -> tuple[UUID, list[ChatMessage], ChatMessage]:
    """Bind ``thread`` to a PDF with a selected path and one unselected sibling.

    The path alternates user/assistant turns, each parented on the one before.
    The sibling is a later user message under the same parent as ``path[2]``,
    so it is live but off the selected path.
    """
    document = Document(
        id=uuid4(),
        organization_id=user.organization_id,
        uploaded_by_user_id=user.id,
        title="Paper",
        filename="paper.pdf",
        file_path="paper.pdf",
        file_size_bytes=1,
        mime_type="application/pdf",
        document_type=DocumentType.PDF,
        processing_status=ProcessingStatus.COMPLETED,
    )
    db.add(document)
    await db.commit()
    start = datetime.utcnow() - timedelta(hours=1)
    path: list[ChatMessage] = []
    for index in range(path_length):
        created = start + timedelta(milliseconds=index * 10)
        row = ChatMessage(
            id=uuid4(),
            thread_id=thread.id,
            user_id=user.id,
            role=MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT,
            content=f"path-{index}",
            token_count=1,
            parent_message_id=path[-1].id if path else None,
            created_at=created,
            updated_at=created,
        )
        db.add(row)
        path.append(row)
    created = start + timedelta(milliseconds=path_length * 10)
    sibling = ChatMessage(
        id=uuid4(),
        thread_id=thread.id,
        user_id=user.id,
        role=MessageRole.USER,
        content="off-path",
        token_count=1,
        parent_message_id=path[1].id,
        created_at=created,
        updated_at=created,
    )
    db.add(sibling)
    await db.commit()
    db.info["_created"]["chat_messages"].extend(row.id for row in [*path, sibling])
    thread.message_branching = True
    thread.branch_document_id = document.id
    thread.selected_message_id = path[-1].id
    await db.commit()
    return document.id, path, sibling


async def _drop_document(db: AsyncSession, document_id: UUID) -> None:
    """``documents`` is not in the fixture cleanup; the thread FK is SET NULL."""
    await db.rollback()
    await db.execute(delete(Document).where(Document.id == document_id))
    await db.commit()


def _client(db: AsyncSession, user: User) -> AsyncClient:
    app = FastAPI()
    app.include_router(threads_routes.router, prefix="/api/v2")

    async def session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@contextmanager
def _capture(engine: AsyncEngine) -> Iterator[dict[str, Any]]:
    """Record SQL statements and the number of ``ChatMessage`` rows loaded."""
    seen: dict[str, Any] = {"statements": [], "rows": 0}

    def on_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        seen["statements"].append(statement)

    def on_load(target: Any, context: Any, *args: Any) -> None:
        seen["rows"] += 1

    event.listen(engine.sync_engine, "before_cursor_execute", on_execute)
    event.listen(ChatMessage, "load", on_load)
    event.listen(ChatMessage, "refresh", on_load)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", on_execute)
        event.remove(ChatMessage, "load", on_load)
        event.remove(ChatMessage, "refresh", on_load)


def _message_row_selects(statements: list[str]) -> list[str]:
    """SELECTs that return ``chat_messages`` rows, as opposed to a count."""
    return [
        sql
        for sql in statements
        if sql.lstrip().upper().startswith("SELECT")
        and "FROM chat_messages" in sql
        and "count(" not in sql.lower()
    ]


async def _reference_context(
    db: AsyncSession,
    thread_id: Any,
    *,
    max_messages: Optional[int] = None,
    max_tokens: Optional[int] = None,
) -> dict:
    """The pre-GOO-712 algorithm, applied to the thread's full ordered rows."""
    effective_max_messages = max_messages or settings.THREAD_DEFAULT_MAX_MESSAGES
    effective_max_tokens = max_tokens or settings.THREAD_DEFAULT_MAX_TOKENS
    all_rows = list(
        (
            await db.scalars(
                select(ChatMessage)
                .where(ChatMessage.thread_id == thread_id)
                .order_by(ChatMessage.created_at.asc())
            )
        ).all()
    )
    messages: list[dict] = []
    total_tokens = 0
    all_message_count = len(
        [m for m in all_rows if not m.is_deleted and m.superseded_by_message_id is None]
    )
    truncated = False
    for msg in reversed(all_rows):
        if msg.is_deleted:
            continue
        if msg.superseded_by_message_id is not None:
            continue
        msg_tokens = msg.token_count if msg.token_count else 0
        if total_tokens + msg_tokens > effective_max_tokens:
            truncated = True
            break
        messages.insert(0, msg.to_llm_format())
        total_tokens += msg_tokens
        if len(messages) >= effective_max_messages:
            truncated = True
            break
    usage_ratio = total_tokens / effective_max_tokens if effective_max_tokens > 0 else 0
    return {
        "messages": messages,
        "metadata": {
            "truncated": truncated,
            "total_tokens": total_tokens,
            "max_tokens": effective_max_tokens,
            "message_count": len(messages),
            "total_messages": all_message_count,
            "usage_ratio": round(usage_ratio, 2),
            "approaching_limit": usage_ratio >= settings.THREAD_CONTEXT_WARN_THRESHOLD,
        },
    }


# --------------------------------------------------------------------------- #
# (a) + (b): the reads are bounded in SQL
# --------------------------------------------------------------------------- #


async def test_thread_context_loads_only_the_newest_max_messages(
    _engine: AsyncEngine,
    db_session: AsyncSession,
    thread_factory: ThreadFactory,
    user_factory: UserFactory,
) -> None:
    user = await user_factory()
    thread = await thread_factory(user=user)
    rows = await _seed(db_session, thread, user, [10] * 30)
    newest = [row.to_llm_format() for row in rows[-5:]]
    # Empty identity map: every message the service reads is a fresh load.
    db_session.expunge_all()

    with _capture(_engine) as seen:
        context = await ChatService(db_session).get_thread_context(
            thread.id, user.id, max_messages=5
        )

    selects = _message_row_selects(seen["statements"])
    assert selects, "the context read issued no chat_messages select"
    assert all("LIMIT" in sql.upper() for sql in selects), selects
    assert seen["rows"] <= 5 + 1
    assert context is not None
    assert context["messages"] == newest
    assert context["metadata"]["total_messages"] == 30
    assert context["metadata"]["truncated"] is True
    assert context["metadata"]["message_count"] == 5


async def test_thread_detail_returns_newest_page_and_flags_older_messages(
    _engine: AsyncEngine,
    db_session: AsyncSession,
    thread_factory: ThreadFactory,
    user_factory: UserFactory,
) -> None:
    user = await user_factory()
    thread = await thread_factory(user=user)
    rows = await _seed(db_session, thread, user, [1] * (THREAD_DETAIL_MAX_MESSAGES + 3))
    expected_ids = [row.id for row in rows[-THREAD_DETAIL_MAX_MESSAGES:]]
    db_session.expunge_all()

    with _capture(_engine) as seen:
        detail = await ChatService(db_session).get_thread_with_recent_messages(
            thread.id, user.id
        )

    assert detail is not None
    loaded, has_more = detail
    assert has_more is True
    assert [m.id for m in loaded.messages] == expected_ids
    selects = _message_row_selects(seen["statements"])
    assert selects, "the detail read issued no chat_messages select"
    assert all("LIMIT" in sql.upper() for sql in selects), selects
    assert seen["rows"] <= THREAD_DETAIL_MAX_MESSAGES + 1


# --------------------------------------------------------------------------- #
# (c) + (d): small threads read exactly as before
# --------------------------------------------------------------------------- #


async def test_thread_context_matches_full_load_for_small_threads(
    db_session: AsyncSession,
    thread_factory: ThreadFactory,
    user_factory: UserFactory,
) -> None:
    user = await user_factory()
    thread = await thread_factory(user=user)
    await _small_thread(db_session, thread, user)
    service = ChatService(db_session)

    # Everything fits; then the token budget truncates (500 fits, 500 + 300
    # does not); then the message cap is hit exactly.
    limit_cases: list[tuple[Optional[int], Optional[int]]] = [
        (None, None),
        (None, 600),
        (3, None),
    ]
    for max_messages, max_tokens in limit_cases:
        context = await service.get_thread_context(
            thread.id, user.id, max_messages=max_messages, max_tokens=max_tokens
        )
        reference = await _reference_context(
            db_session, thread.id, max_messages=max_messages, max_tokens=max_tokens
        )
        assert context == reference, (max_messages, max_tokens)

    fits = await service.get_thread_context(thread.id, user.id)
    assert fits is not None
    assert fits["metadata"]["truncated"] is False
    assert fits["metadata"]["total_messages"] == 3
    assert [m["content"] for m in fits["messages"]] == ["msg-0", "msg-2", "msg-4"]
    cut = await service.get_thread_context(thread.id, user.id, max_tokens=600)
    assert cut is not None
    assert cut["metadata"]["truncated"] is True
    assert [m["content"] for m in cut["messages"]] == ["msg-4"]
    capped = await service.get_thread_context(thread.id, user.id, max_messages=3)
    assert capped is not None
    assert capped["metadata"]["truncated"] is True
    assert capped["metadata"]["message_count"] == 3


async def test_thread_context_is_none_for_missing_or_foreign_thread(
    db_session: AsyncSession,
    thread_factory: ThreadFactory,
    user_factory: UserFactory,
) -> None:
    user = await user_factory()
    thread = await thread_factory(user=user)
    stranger = await user_factory()
    service = ChatService(db_session)

    assert await service.get_thread_context(uuid4(), user.id) is None
    assert await service.get_thread_context(thread.id, stranger.id) is None


async def test_thread_detail_route_matches_full_load_for_small_threads(
    db_session: AsyncSession,
    thread_factory: ThreadFactory,
    user_factory: UserFactory,
) -> None:
    user = await user_factory()
    thread = await thread_factory(user=user)
    rows = await _small_thread(db_session, thread, user)

    # The pre-GOO-712 read: full relationship load, filtered in Python.
    old = await thread_service.get_thread_detail(db_session, thread.id, user.id)
    assert old is not None
    old_ids = [
        str(m.id)
        for m in old.messages
        if not m.is_deleted and m.superseded_by_message_id is None
    ]
    assert old_ids == [str(rows[i].id) for i in (0, 2, 4)]

    async with _client(db_session, user) as client:
        response = await client.get(f"/api/v2/threads/{thread.id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert [m["id"] for m in body["messages"]] == old_ids
        assert body["has_more_messages"] is False

        bare = await client.get(
            f"/api/v2/threads/{thread.id}", params={"include_messages": "false"}
        )
        assert bare.status_code == 200, bare.text
        assert bare.json()["messages"] == []
        assert bare.json()["has_more_messages"] is False


# --------------------------------------------------------------------------- #
# Branched threads: the newest page of the selected path, paged by its head
# --------------------------------------------------------------------------- #


async def test_branched_thread_detail_and_context_page_the_selected_path(
    db_session: AsyncSession,
    thread_factory: ThreadFactory,
    user_factory: UserFactory,
) -> None:
    user = await user_factory()
    thread = await thread_factory(user=user)
    document_id, path, sibling = await _branched_thread(
        db_session, thread, user, THREAD_DETAIL_MAX_MESSAGES + 3
    )
    path_ids = [row.id for row in path]
    newest_context = [row.to_llm_format() for row in path[-5:]]
    try:
        detail = await ChatService(db_session).get_thread_with_recent_messages(
            thread.id, user.id
        )
        assert detail is not None
        loaded, has_more = detail
        assert has_more is True
        assert [m.id for m in loaded.messages] == path_ids[-THREAD_DETAIL_MAX_MESSAGES:]
        assert sibling.id not in {m.id for m in loaded.messages}
        # What a client keeps from the response; later reads refresh the row.
        oldest_id, head_id = loaded.messages[0].id, loaded.selected_message_id
        assert head_id == path_ids[-1]

        # The older path rows page through ``/messages`` with the head pinned.
        older = await message_service.list_messages(
            db_session,
            thread.id,
            user.id,
            limit=THREAD_DETAIL_MAX_MESSAGES,
            order="desc",
            before_id=oldest_id,
            selected_message_id=head_id,
        )
        assert older is not None
        assert [m.id for m in older[0]] == list(reversed(path_ids[:3]))
        assert older[2] is False
        with pytest.raises(BranchAccessError) as unpinned:
            await message_service.list_messages(
                db_session,
                thread.id,
                user.id,
                order="desc",
                before_id=oldest_id,
            )
        assert unpinned.value.status_code == 409

        context = await ChatService(db_session).get_thread_context(
            thread.id, user.id, max_messages=5
        )
        assert context is not None
        assert context["messages"] == newest_context
        assert context["metadata"]["total_messages"] == len(path)
        assert context["metadata"]["truncated"] is True

        async with _client(db_session, user) as client:
            response = await client.get(f"/api/v2/threads/{thread.id}")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["has_more_messages"] is True
            assert body["selected_message_id"] == str(path_ids[-1])
            assert [m["id"] for m in body["messages"]] == [
                str(i) for i in path_ids[-THREAD_DETAIL_MAX_MESSAGES:]
            ]
    finally:
        await _drop_document(db_session, document_id)


async def test_branched_context_maps_branch_errors_like_thread_detail(
    db_session: AsyncSession,
    thread_factory: ThreadFactory,
    user_factory: UserFactory,
) -> None:
    user = await user_factory()
    thread = await thread_factory(user=user)
    document_id, path, _sibling = await _branched_thread(db_session, thread, user, 4)
    # A soft-deleted ancestor breaks the selected path: ``read_thread`` raises
    # ``BranchAccessError``, which both routes must answer with its status.
    path[1].is_deleted = True
    await db_session.commit()
    try:
        async with _client(db_session, user) as client:
            detail = await client.get(f"/api/v2/threads/{thread.id}")
            context = await client.get(f"/api/v2/threads/{thread.id}/context")
        assert detail.status_code == 404, detail.text
        assert context.status_code == detail.status_code, context.text
        assert context.json()["detail"] == detail.json()["detail"]
    finally:
        await _drop_document(db_session, document_id)
