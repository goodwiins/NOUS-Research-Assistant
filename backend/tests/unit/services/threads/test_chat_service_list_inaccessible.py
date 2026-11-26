"""ChatService list wrappers keep the leaf's "not found / no access" ``None``.

The leaves (``conversation_service.list_conversations``,
``thread_service.list_threads``, ``message_service.list_messages``) return
``None`` when the parent is missing or not accessible, distinct from an empty
page. The ChatService wrappers used to collapse it into ``([], 0)``, so the
legacy routes answered 200 with an empty page (B8-S5). They now pass ``None``
through and the routes map it to 404.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.threads import conversation_service, message_service, thread_service
from src.services.threads.chat_service import ChatService

pytestmark = pytest.mark.unit

# (leaf module, method name, leaf result for an accessible empty parent)
_WRAPPERS: list[tuple[Any, str, tuple]] = [
    (conversation_service, "list_conversations", ([], 0, {})),
    (thread_service, "list_threads", ([], 0, {})),
    (message_service, "list_messages", ([], 0, False)),
]


async def _call(module: Any, name: str, leaf_result: Any) -> Any:
    service = ChatService(MagicMock(name="AsyncSession"))
    with patch.object(module, name, new=AsyncMock(return_value=leaf_result)):
        return await getattr(service, name)(uuid.uuid4(), uuid.uuid4())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "name", "_empty"), _WRAPPERS, ids=[w[1] for w in _WRAPPERS]
)
async def test_wrapper_returns_none_when_leaf_returns_none(
    module: Any, name: str, _empty: tuple
) -> None:
    assert await _call(module, name, None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "name", "empty"), _WRAPPERS, ids=[w[1] for w in _WRAPPERS]
)
async def test_wrapper_returns_empty_page_for_accessible_parent(
    module: Any, name: str, empty: tuple
) -> None:
    assert await _call(module, name, empty) == ([], 0)
