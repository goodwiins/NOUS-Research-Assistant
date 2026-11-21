"""IDOR guard for ``create_message`` citations (GOO-631, finding API2C-1).

``POST /api/v2/messages`` accepts ``citations[].document_id`` from the client.
The presenter falls back to ``citation.document.title`` / ``document_type``, so
storing a guessed foreign document UUID leaked that document's title and type.
Attachments in the same method already went through the org-owned filter;
citations must too. A citation whose ``document_id`` the caller's organization
does not own keeps its client-supplied fields but loses the document link.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Sequence
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from src.models.chat_message import ChatMessage
from src.models.citation import Citation
from src.models.conversation import Conversation
from src.models.thread import Thread
from src.models.workspace import Workspace
from src.schemas.chat import ChatMessageCreate, CitationCreate, MessageRole
from src.services.threads.chat_service import ChatService

pytestmark = pytest.mark.unit


@pytest.fixture
async def db() -> AsyncGenerator[tuple[AsyncSession, uuid.UUID, uuid.UUID], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Thread.__table__.create)
        await conn.run_sync(ChatMessage.__table__.create)
        await conn.run_sync(Workspace.__table__.create)
        await conn.run_sync(Conversation.__table__.create)
        await conn.run_sync(Citation.__table__.create)

    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        session.add(
            Workspace(
                id=workspace_id,
                name="workspace",
                owner_id=user_id,
                organization_id=uuid.uuid4(),
                members=[],
            )
        )
        session.add(
            Conversation(
                id=conversation_id,
                workspace_id=workspace_id,
                title="conversation",
                created_by_id=user_id,
            )
        )
        session.add(
            Thread(id=thread_id, conversation_id=conversation_id, created_by_id=user_id)
        )
        await session.commit()
        yield session, user_id, thread_id
    await engine.dispose()


async def _create_with_citations(
    session: AsyncSession,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    citations: list[CitationCreate],
    owned: Sequence[uuid.UUID],
) -> tuple[ChatMessage, AsyncMock]:
    service = ChatService(session)
    thread = (
        await session.execute(
            select(Thread)
            .options(
                selectinload(Thread.conversation).selectinload(Conversation.workspace)
            )
            .where(Thread.id == thread_id)
        )
    ).scalar_one()
    set_committed_value(thread.conversation.workspace, "members", [])

    async def _owned_only(ids: Sequence[uuid.UUID], _user_id: uuid.UUID) -> list:
        return [i for i in ids if i in set(owned)]

    owned_filter = AsyncMock(side_effect=_owned_only)
    with (
        patch.object(service, "get_thread", new=AsyncMock(return_value=thread)),
        patch.object(service, "_filter_owned_document_ids", new=owned_filter),
    ):
        message = await service.create_message(
            ChatMessageCreate(
                thread_id=thread_id,
                role=MessageRole.ASSISTANT,
                content="answer",
                citations=citations,
            ),
            user_id,
        )
    assert message is not None
    return message, owned_filter


async def _stored_citations(session: AsyncSession, message_id: uuid.UUID) -> list:
    rows = await session.execute(
        select(Citation)
        .where(Citation.message_id == message_id)
        .order_by(Citation.chunk_index)
    )
    return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_foreign_citation_document_id_is_not_linked(
    db: tuple[AsyncSession, uuid.UUID, uuid.UUID],
) -> None:
    session, user_id, thread_id = db
    foreign_doc = uuid.uuid4()

    message, owned_filter = await _create_with_citations(
        session,
        user_id,
        thread_id,
        [CitationCreate(document_id=foreign_doc, chunk_index=0, snippet="s")],
        owned=[],
    )

    owned_filter.assert_awaited_once_with([foreign_doc], user_id)
    stored = await _stored_citations(session, message.id)
    assert len(stored) == 1
    assert stored[0].document_id is None
    assert stored[0].snippet == "s"


@pytest.mark.asyncio
async def test_owned_citation_document_id_is_kept_in_mixed_batch(
    db: tuple[AsyncSession, uuid.UUID, uuid.UUID],
) -> None:
    session, user_id, thread_id = db
    owned_doc = uuid.uuid4()
    foreign_doc = uuid.uuid4()

    message, _ = await _create_with_citations(
        session,
        user_id,
        thread_id,
        [
            CitationCreate(document_id=owned_doc, chunk_index=0),
            CitationCreate(document_id=foreign_doc, chunk_index=1),
            CitationCreate(external_reference_id="arxiv:1234.5678", chunk_index=2),
        ],
        owned=[owned_doc],
    )

    stored = await _stored_citations(session, message.id)
    assert [c.document_id for c in stored] == [owned_doc, None, None]
    assert stored[2].external_reference_id == "arxiv:1234.5678"


@pytest.mark.asyncio
async def test_citations_without_document_ids_skip_the_ownership_query(
    db: tuple[AsyncSession, uuid.UUID, uuid.UUID],
) -> None:
    session, user_id, thread_id = db

    message, owned_filter = await _create_with_citations(
        session,
        user_id,
        thread_id,
        [CitationCreate(external_reference_id="doi:10.1/x", chunk_index=0)],
        owned=[],
    )

    owned_filter.assert_not_awaited()
    assert len(await _stored_citations(session, message.id)) == 1
