"""Two-account IDOR check for ``create_message`` citations (GOO-631, API2C-1).

Mirrors ``test_message_attachment_ownership``: a citation that names another
organization's document must not link to it, or the message presenter would
return that document's title and type.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from src.models import Citation, Document, DocumentType, MessageRole
from src.schemas.chat import ChatMessageCreate, CitationCreate
from src.services.threads.chat_service import ChatService

pytestmark = pytest.mark.integration


async def _seed_document(db_session, organization_id) -> Document:
    doc = Document(
        id=uuid4(),
        title="secret-title",
        filename="secret.pdf",
        file_path="s3://bucket/secret.pdf",
        file_size_bytes=123,
        mime_type="application/pdf",
        document_type=DocumentType.PDF,
        organization_id=organization_id,
        is_deleted=False,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)
    return doc


async def test_foreign_org_citation_is_unlinked_and_owned_kept(
    db_session, thread_factory, user_factory, organization_factory
):
    org_b = await organization_factory()
    foreign_doc = await _seed_document(db_session, org_b.id)

    user_a = await user_factory()
    owned_doc = await _seed_document(db_session, user_a.organization_id)
    thread = await thread_factory(user=user_a)

    message = await ChatService(db_session).create_message(
        ChatMessageCreate(
            thread_id=thread.id,
            content="answer",
            role=MessageRole.ASSISTANT,
            citations=[
                CitationCreate(document_id=foreign_doc.id, chunk_index=0),
                CitationCreate(document_id=owned_doc.id, chunk_index=1),
            ],
        ),
        user_id=user_a.id,
    )

    assert message is not None
    rows = await db_session.execute(
        select(Citation.document_id)
        .where(Citation.message_id == message.id)
        .order_by(Citation.chunk_index)
    )
    assert list(rows.scalars().all()) == [None, owned_doc.id]
