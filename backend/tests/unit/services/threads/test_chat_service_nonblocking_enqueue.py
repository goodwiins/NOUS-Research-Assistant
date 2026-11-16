"""GOO-466: ChatService's async bulk handlers must not publish on the loop.

Celery ``.delay()`` / ``.apply_async()`` block on the broker round-trip. Called
directly inside an ``async def`` they stall the event loop (every concurrent
request) whenever Redis is slow. The publishes must go through
``asyncio.to_thread(task.delay, ...)`` instead, which passes ``delay`` as a
value rather than calling it on the loop thread.

Static AST check (precedent: ``test_transaction_ownership.py``): no direct call
to a blocking Celery publish method inside any async ``ChatService`` method.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CHAT_SERVICE = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "services"
    / "threads"
    / "chat_service.py"
)
_BLOCKING_PUBLISH = {"delay", "apply_async", "send_task"}


def _async_methods() -> list[ast.AsyncFunctionDef]:
    tree = ast.parse(CHAT_SERVICE.read_text())
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ChatService"
    )
    return [node for node in cls.body if isinstance(node, ast.AsyncFunctionDef)]


def test_bulk_handlers_are_present() -> None:
    names = {fn.name for fn in _async_methods()}
    assert {"bulk_update_threads", "bulk_summarize_threads"} <= names


@pytest.mark.parametrize("method", _async_methods(), ids=lambda fn: fn.name)
def test_no_blocking_celery_publish_on_the_event_loop(
    method: ast.AsyncFunctionDef,
) -> None:
    offenders = [
        f"line {node.lineno}: .{node.func.attr}()"
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _BLOCKING_PUBLISH
    ]
    assert not offenders, (
        f"ChatService.{method.name} calls a blocking Celery publish on the event "
        f"loop ({', '.join(offenders)}); wrap it in asyncio.to_thread(...)"
    )
