"""Legacy list routes answer 404 for a missing or forbidden parent (B8-S5).

``ChatService.list_threads`` / ``list_messages`` / ``list_conversations``
return ``None`` when the parent is missing or the caller has no access. The
routes used to unpack that as an empty page and answer 200, so a forbidden
thread looked like an empty one. They now raise 404 with one detail for both
cases (no existence oracle), matching the R5-M10 ``/context`` route, while an
accessible parent with no rows still answers 200 with an empty page.

Focused command:
  pytest -q backend/tests/api/threads/test_legacy_list_inaccessible_404.py
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.threads import conversations as conversations_module
from src.api.threads import threads as threads_module
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.main import app
from src.services.threads.chat_service import ChatService
from tests.conftest import _patch_multi_tenancy_middleware, _test_auth_headers

pytestmark = pytest.mark.unit


class _MockUser:
    id = uuid4()
    email = "t@example.com"


def _service(method: str, result: Optional[tuple]) -> AsyncMock:
    service = AsyncMock(spec=ChatService)
    setattr(service, method, AsyncMock(return_value=result))
    return service


@contextmanager
def _client(
    monkeypatch: pytest.MonkeyPatch, service: AsyncMock
) -> Iterator[TestClient]:
    # Not the shared ``test_app`` fixture: it replaces
    # ``app.dependency_overrides`` with a new dict, which drops the overrides
    # other unit modules (tests/evidence/test_api.py) install at import time.
    # Snapshot them here and put them back afterwards instead.
    _patch_multi_tenancy_middleware(monkeypatch)
    monkeypatch.setattr(threads_module, "get_chat_service", lambda _db: service)
    saved_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = lambda: _MockUser()
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    @asynccontextmanager
    async def _no_lifespan(_app: Any) -> AsyncIterator[None]:
        yield

    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = _no_lifespan
    try:
        with TestClient(app, headers=_test_auth_headers()) as client:
            yield client
    finally:
        app.router.lifespan_context = original_lifespan
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved_overrides)


def test_list_messages_inaccessible_thread_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service("list_messages", None)
    with _client(monkeypatch, service) as client:
        resp = client.get(f"/api/v2/threads/{uuid4()}/messages")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["message"] == "Thread not found or access denied"


def test_list_messages_empty_accessible_thread_is_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service("list_messages", ([], 0))
    with _client(monkeypatch, service) as client:
        resp = client.get(f"/api/v2/threads/{uuid4()}/messages")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["messages"] == []
    assert body["total"] == 0
    assert body["has_more"] is False


def test_list_threads_inaccessible_conversation_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service("list_threads", None)
    with _client(monkeypatch, service) as client:
        resp = client.get("/api/v2/threads", params={"conversation_id": str(uuid4())})

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["message"] == "Conversation not found or access denied"


def test_list_threads_empty_accessible_conversation_is_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service("list_threads", ([], 0))
    with _client(monkeypatch, service) as client:
        resp = client.get("/api/v2/threads", params={"conversation_id": str(uuid4())})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["threads"] == []
    assert body["total"] == 0


# The conversations router is not mounted in main.py, so its handler is called
# directly: it must answer 404 instead of crashing on ``None``.


def _call_list_conversations(
    monkeypatch: pytest.MonkeyPatch, result: Optional[tuple]
) -> Any:
    service = _service("list_conversations", result)
    monkeypatch.setattr(conversations_module, "get_chat_service", lambda _db: service)
    user = MagicMock()
    user.id = uuid4()
    return asyncio.run(
        conversations_module.list_conversations(
            workspace_id=uuid4(),
            include_archived=False,
            search=None,
            limit=50,
            offset=0,
            current_user=user,
            db=MagicMock(),
        )
    )


def test_list_conversations_inaccessible_workspace_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _call_list_conversations(monkeypatch, None)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Workspace not found or access denied"


def test_list_conversations_empty_accessible_workspace_is_empty_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _call_list_conversations(monkeypatch, ([], 0))

    assert response.conversations == []
    assert response.total == 0
