"""Named-document retrieval while the Bedrock queue has not indexed a document.

With the Bedrock write path on, a requested document that is not
``do_kb_sync_status='completed'`` is answered from organization-scoped
PostgreSQL full-text instead of an empty vector search, and the payload says
so. Authorization still runs first.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from src.services import bedrock_sync
from src.services.agent import tools_impl
from src.services.agent.tools_impl import _tool_do_kb_retrieve
from src.services.do_kb.models import Chunk, RetrieveResult
from src.services.do_kb.retrieval import DOKBRetrieveOutcome, DOKBRetrieveStatus

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TARGET_ID = UUID("11111111-1111-4111-8111-111111111111")


def _rows(rows: list[Any]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = rows
    result.scalars.return_value.all.return_value = rows
    return result


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        DO_KB_ENABLED=False,
        DO_KB_DEFAULT_TOP_K=8,
        AGENT_DOKB_COHERE_RERANK=False,
        AGENT_ITERATIVE_RETRIEVAL=False,
        BEDROCK_KB_ID="KBID",
    )


async def _call(db: MagicMock, *, sync_on: bool = True) -> tuple[dict, AsyncMock, Any]:
    provider = AsyncMock(
        return_value=DOKBRetrieveOutcome(
            status=DOKBRetrieveStatus.SUCCESS,
            result=RetrieveResult(
                chunks=[
                    Chunk(
                        text="vector hit",
                        score=0.9,
                        document_id=str(TARGET_ID),
                        metadata={"score_source": "upstream"},
                    )
                ],
                total=1,
            ),
        )
    )
    local = AsyncMock(
        return_value=[{"document_id": str(TARGET_ID), "text": "keyword hit"}]
    )
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    with (
        patch("src.core.config.settings", _settings()),
        patch.object(bedrock_sync.settings, "BEDROCK_KB_ID", "KBID"),
        patch.object(
            bedrock_sync.settings,
            "BEDROCK_KB_DATA_SOURCE_ID",
            "DSID" if sync_on else "",
        ),
        patch(
            "src.core.database.engine",
            SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        ),
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value="KBID"),
        ),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
        patch.object(tools_impl, "_local_fulltext_chunks", local),
    ):
        result = await _tool_do_kb_retrieve(
            {"query": "rates", "document_ids": [str(TARGET_ID)]},
            db,
            user,  # type: ignore[arg-type]
        )
    return result, provider, local


async def test_unindexed_document_falls_back_to_keyword_search_with_note() -> None:
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _rows([(TARGET_ID, None, "s3")]),
            _rows([("pending", {"bedrock_sync": {"stage_token": "t"}})]),
        ]
    )

    result, provider, local = await _call(db)

    provider.assert_not_awaited()
    assert local.await_args.kwargs["document_ids"] == [TARGET_ID]
    assert result["source"] == "postgres_fulltext"
    assert result["indexing_pending"] is True
    assert result["note"].startswith("The requested document is still being indexed")
    assert result["chunks"] == [{"document_id": str(TARGET_ID), "text": "keyword hit"}]
    status_sql = str(db.execute.await_args_list[1].args[0]).lower()
    assert "documents.organization_id" in status_sql
    assert "documents.is_deleted = false" in status_sql


async def test_completed_status_from_retired_kb_is_not_trusted() -> None:
    """``do_kb_sync_status`` is shared with the DigitalOcean KB: a completed
    row that this queue never staged is absent from Bedrock, so it must use
    keyword search instead of returning nothing."""
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[_rows([(TARGET_ID, None, "s3")]), _rows([("completed", None)])]
    )

    result, provider, local = await _call(db)

    provider.assert_not_awaited()
    local.assert_awaited_once()
    assert result["indexing_pending"] is True


async def test_indexed_document_uses_bedrock() -> None:
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _rows([(TARGET_ID, None, "s3")]),
            _rows([("completed", {"bedrock_sync": {"stage_token": "t"}})]),
            _rows([(TARGET_ID, None, "Rate policy")]),  # chunk -> document
        ]
    )

    result, provider, local = await _call(db)

    provider.assert_awaited_once()
    local.assert_not_awaited()
    assert "indexing_pending" not in result


async def test_read_only_bedrock_does_not_consult_sync_status() -> None:
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _rows([(TARGET_ID, None, "s3")]),
            _rows([(TARGET_ID, None, "Rate policy")]),  # chunk -> document
        ]
    )

    _, provider, local = await _call(db, sync_on=False)

    statements = [str(c.args[0]).lower() for c in db.execute.await_args_list]
    assert not any("do_kb_sync_status" in sql for sql in statements)
    provider.assert_awaited_once()
    local.assert_not_awaited()


async def test_foreign_document_rejected_before_status_or_fallback() -> None:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_rows([])])

    result, provider, local = await _call(db)

    assert result["reason"] == "requested_documents_unavailable"
    assert db.execute.await_count == 1
    provider.assert_not_awaited()
    local.assert_not_awaited()
