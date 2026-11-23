"""AnalyticsCache must actually connect to Redis (GOO-654, CI-4).

``__init__`` used to call the async ``_init_redis()`` without awaiting it, so
the coroutine never ran, ``_connected`` stayed False, and every pod used its
own in-memory fallback. The cache now connects lazily on first use.
"""

from __future__ import annotations

import warnings
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.cache import analytics_cache as module

pytestmark = pytest.mark.unit


def _cache(monkeypatch: pytest.MonkeyPatch, client: Any) -> module.AnalyticsCache:
    monkeypatch.setattr(module, "REDIS_AVAILABLE", True)
    monkeypatch.setattr(
        module,
        "get_analytics_config",
        lambda: MagicMock(redis_analytics_url="redis://example:6379/0"),
    )
    monkeypatch.setattr(module.redis, "from_url", MagicMock(return_value=client))
    return module.AnalyticsCache()


def test_constructor_does_not_leave_an_unawaited_coroutine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        cache = _cache(monkeypatch, AsyncMock())
        del cache
        import gc

        gc.collect()


@pytest.mark.asyncio
async def test_first_get_connects_and_reads_from_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    client.get.return_value = '{"v": 1}'
    cache = _cache(monkeypatch, client)
    assert cache._connected is False

    assert await cache.get("k") == {"v": 1}

    assert cache._connected is True
    client.ping.assert_awaited_once()
    client.get.assert_awaited_once_with("k")


@pytest.mark.asyncio
async def test_set_writes_through_to_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AsyncMock()
    cache = _cache(monkeypatch, client)

    await cache.set("k", {"v": 1}, ttl=60)

    client.setex.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_connect_is_not_retried_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    client.ping.side_effect = ConnectionError("down")
    cache = _cache(monkeypatch, client)

    await cache.get("a")
    await cache.get("b")

    assert cache._connected is False
    assert client.ping.await_count == 1  # second call is inside the backoff

    cache._next_connect_attempt = 0.0
    client.ping.side_effect = None
    await cache.get("c")
    assert cache._connected is True


@pytest.mark.asyncio
async def test_disabled_redis_never_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "REDIS_AVAILABLE", False)
    cache = module.AnalyticsCache()

    assert await cache.connect() is False
    await cache.set("k", 1, ttl=5)
    assert await cache.get("k") == 1  # in-memory fallback still works
