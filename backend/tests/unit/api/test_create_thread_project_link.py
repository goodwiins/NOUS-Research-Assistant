"""``POST /api/v2/threads`` with ``project_id`` (audit API-1).

The auto-link branch logged with structlog-style keyword arguments on a stdlib
``logging`` logger. ``Logger.info`` rejects unknown keywords with
``TypeError``, so the request 500'd and the flushed-but-uncommitted thread was
rolled back. Both branches (project found / not found) are pinned here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.threads import threads as threads_mod
from src.schemas.chat import ThreadCreate

pytestmark = pytest.mark.unit


def _thread() -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        title="t",
        summary=None,
        status="active",
        last_message_at=now,
        message_count=0,
        token_count=0,
        created_by_id=uuid.uuid4(),
        created_at=now,
        updated_at=now,
        source_project_id=None,
    )


@pytest.mark.parametrize("project_found", [True, False], ids=["linked", "missing"])
async def test_create_thread_with_project_id_commits(
    monkeypatch: pytest.MonkeyPatch, project_found: bool
) -> None:
    thread = _thread()
    service = MagicMock()
    service.create_thread = AsyncMock(return_value=thread)
    monkeypatch.setattr(threads_mod, "get_chat_service", lambda db: service)
    attach = AsyncMock()
    monkeypatch.setattr(
        "src.services.research.project_thread_service.attach_thread_to_project",
        attach,
    )
    monkeypatch.setattr(
        threads_mod.thread_event_service, "broadcast_thread_created", AsyncMock()
    )

    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = (
        SimpleNamespace(id=uuid.uuid4()) if project_found else None
    )
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    user: Any = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())
    data = ThreadCreate(
        conversation_id=thread.conversation_id, title="t", project_id=uuid.uuid4()
    )

    resp = await threads_mod.create_thread(data=data, current_user=user, db=db)

    assert resp.id == thread.id
    db.commit.assert_awaited_once()
    assert attach.await_count == (1 if project_found else 0)
