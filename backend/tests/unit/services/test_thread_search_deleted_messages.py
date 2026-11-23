"""Soft-deleted chat messages must not be searchable (GOO-635, finding IS-3).

The thread/message search builders excluded deleted threads, conversations
and workspaces but not deleted messages, so a message the user deleted still
matched and its content came back in the results. Every builder that reads
``chat_messages`` (result and count, plus the inline ``combined_search`` SQL
and the thread builders' message subqueries) must filter it.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from src.services.threads.thread_message_search_service import (
    MessageSearchRequest,
    ThreadMessageSearchService,
    ThreadSearchRequest,
)

pytestmark = pytest.mark.unit

# ``wm.is_deleted`` (workspace members) contains ``m.is_deleted``; anchor it.
_MESSAGE_NOT_DELETED = re.compile(r"(?<![\w.])m\.is_deleted = false")


def _svc() -> ThreadMessageSearchService:
    return ThreadMessageSearchService()


def test_message_search_excludes_deleted_messages() -> None:
    sql, _ = _svc()._build_message_search_query(
        "neural", MessageSearchRequest(query="neural"), uuid4()
    )
    assert _MESSAGE_NOT_DELETED.search(sql)


def test_message_count_excludes_deleted_messages() -> None:
    sql, _ = _svc()._build_message_count_query(
        "neural", MessageSearchRequest(query="neural"), uuid4()
    )
    assert _MESSAGE_NOT_DELETED.search(sql)


@pytest.mark.parametrize(
    "builder", ["_build_thread_search_query", "_build_thread_count_query"]
)
def test_thread_builders_ignore_deleted_messages(builder: str) -> None:
    """A thread must not match (or count matches) through a deleted message."""
    sql, _ = getattr(_svc(), builder)(
        "neural", ThreadSearchRequest(query="neural"), uuid4()
    )
    message_subqueries = sql.count("FROM chat_messages cm")
    assert message_subqueries >= 1
    assert sql.count("cm.is_deleted = false") == message_subqueries


def test_combined_search_message_cte_excludes_deleted_messages() -> None:
    db = MagicMock()
    executed: dict = {}

    def _capture(clause, params=None):  # type: ignore[no-untyped-def]
        executed["sql"] = str(clause)
        result = MagicMock()
        result.fetchall.return_value = []
        return result

    db.execute.side_effect = _capture
    _svc().combined_search("neural", user_id=uuid4(), db=db)

    _, message_cte = executed["sql"].split("message_matches AS (", 1)
    assert _MESSAGE_NOT_DELETED.search(message_cte)
