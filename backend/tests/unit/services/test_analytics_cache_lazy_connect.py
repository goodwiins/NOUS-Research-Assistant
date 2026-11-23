"""AnalyticsCache connects to Redis, never stalls a request on it, and fails
open (GOO-654, CI-4).

* ``__init__`` used to call the async ``_init_redis()`` without awaiting it,
  so Redis never connected and every pod used its own in-memory cache.
* The cache now connects in the background: a request never waits for a
  connect, and ``connect()`` (startup, warm-up) waits a bounded time.
* Any Redis failure opens a breaker: memory only for a window, then one
  background reconnect. A Redis outage costs one attempt per window.
* Only ``REDIS_URL`` is provided by the deployment, so the cache falls back
  to it when ``ANALYTICS_redis_analytics_url`` is unset.
* Invalidation is tenant-anchored and uses SCAN, never KEYS.
"""

from __future__ import annotations

import asyncio
import fnmatch
import warnings
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.cache import analytics_cache as module

pytestmark = pytest.mark.unit


class FakeRedis:
    """Just enough of redis.asyncio for the cache. ``keys`` must never run."""

    def __init__(self) -> None:
        self.data: Dict[str, str] = {}
        self.ping = AsyncMock()
        self.get_calls = 0
        self.fail_with: Optional[BaseException] = None
        self.closed = False

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    async def get(self, key: str) -> Optional[str]:
        self.get_calls += 1
        self._maybe_fail()
        return self.data.get(key)

    async def setex(self, key: str, _ttl: int, value: str) -> None:
        self._maybe_fail()
        self.data[key] = value

    async def delete(self, *keys: str) -> int:
        self._maybe_fail()
        return sum(1 for k in keys if self.data.pop(k, None) is not None)

    async def scan_iter(self, match: str, count: int = 0) -> AsyncIterator[str]:
        self._maybe_fail()
        for key in list(self.data):
            if fnmatch.fnmatchcase(key, match.replace("\\", "")):
                yield key

    async def keys(self, *_a: Any) -> List[str]:
        raise AssertionError("KEYS blocks the shared Redis; use SCAN")

    async def aclose(self) -> None:
        self.closed = True


def _cache(
    monkeypatch: pytest.MonkeyPatch,
    client: Any,
    url: Optional[str] = "redis://analytics:6379/0",
    app_url: str = "redis://app:6379/0",
) -> tuple[module.AnalyticsCache, MagicMock]:
    monkeypatch.setattr(module, "REDIS_AVAILABLE", True)
    monkeypatch.setattr(
        module, "get_analytics_config", lambda: MagicMock(redis_analytics_url=url)
    )
    monkeypatch.setattr(module.settings, "REDIS_URL", app_url)
    from_url = MagicMock(return_value=client)
    monkeypatch.setattr(module.redis, "from_url", from_url)
    return module.AnalyticsCache(), from_url


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def test_constructor_does_not_leave_an_unawaited_coroutine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        cache, _ = _cache(monkeypatch, FakeRedis())
        del cache
        import gc

        gc.collect()


@pytest.mark.asyncio
async def test_connect_then_reads_and_writes_go_to_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeRedis()
    cache, _ = _cache(monkeypatch, client)

    assert await cache.connect() is True
    await cache.set("k", {"v": 1}, ttl=60)
    assert "k" in client.data
    cache.fallback_cache.clear()
    assert await cache.get("k") == {"v": 1}
    assert client.get_calls == 1


@pytest.mark.asyncio
async def test_first_request_never_waits_for_a_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeRedis()
    gate = asyncio.Event()

    async def black_holed_ping() -> None:
        await gate.wait()

    client.ping.side_effect = black_holed_ping
    cache, _ = _cache(monkeypatch, client)
    await cache.set("k", 1, ttl=60)  # served from memory

    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await cache.get("k") == 1
    assert loop.time() - started < 0.1  # did not wait on the ping
    assert client.get_calls == 0
    gate.set()


@pytest.mark.asyncio
async def test_background_connect_is_used_by_the_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeRedis()
    client.data["k"] = '{"v": 2}'
    cache, _ = _cache(monkeypatch, client)

    assert await cache.get("k") is None  # cold: memory, connect started
    await _settle()
    assert cache._connected is True
    assert await cache.get("k") == {"v": 2}


@pytest.mark.asyncio
async def test_failed_connect_is_retried_once_per_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeRedis()
    client.ping.side_effect = ConnectionError("down")
    cache, from_url = _cache(monkeypatch, client)

    for key in "abcde":
        await cache.get(key)
        await _settle()
    assert from_url.call_count == 1  # one attempt in the window
    assert cache._connected is False

    cache._next_connect_attempt = 0.0  # window elapses
    client.ping.side_effect = None
    await cache.get("f")
    await _settle()
    assert from_url.call_count == 2
    assert cache._connected is True


@pytest.mark.asyncio
async def test_redis_failure_after_connect_opens_the_breaker_and_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeRedis()
    cache, from_url = _cache(monkeypatch, client)
    assert await cache.connect() is True
    await cache.set("k", {"v": 1}, ttl=60)

    client.fail_with = TimeoutError("redis stalled")
    assert await cache.get("k") == {"v": 1}  # fails open to memory
    await _settle()
    assert cache._connected is False
    assert client.closed is True

    calls = client.get_calls
    for _ in range(10):
        assert await cache.get("k") == {"v": 1}
    await _settle()
    assert client.get_calls == calls  # breaker open: Redis not touched
    assert from_url.call_count == 1  # and no reconnect inside the window


@pytest.mark.asyncio
async def test_set_failure_trips_and_still_stores_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeRedis()
    cache, _ = _cache(monkeypatch, client)
    assert await cache.connect() is True
    client.fail_with = ConnectionError("gone")

    assert await cache.set("k", 7, ttl=60) is True
    assert cache._connected is False
    assert await cache.get("k") == 7


@pytest.mark.asyncio
async def test_client_is_built_with_sub_second_timeouts_and_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, from_url = _cache(monkeypatch, FakeRedis())
    await cache.connect()

    kwargs = from_url.call_args.kwargs
    assert 0 < kwargs["socket_connect_timeout"] < 1
    assert 0 < kwargs["socket_timeout"] < 1
    assert kwargs["retry_on_timeout"] is False


@pytest.mark.asyncio
async def test_falls_back_to_the_app_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # The deployment only provides REDIS_URL (redis-credentials Secret).
    cache, from_url = _cache(
        monkeypatch, FakeRedis(), url=None, app_url="rediss://cache.example:6379/0"
    )
    assert await cache.connect() is True
    assert from_url.call_args.args[0] == "rediss://cache.example:6379/0"


@pytest.mark.asyncio
async def test_analytics_url_wins_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    cache, from_url = _cache(monkeypatch, FakeRedis(), url="redis://analytics:1/2")
    await cache.connect()
    assert from_url.call_args.args[0] == "redis://analytics:1/2"


@pytest.mark.asyncio
async def test_disabled_redis_never_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "REDIS_AVAILABLE", False)
    cache = module.AnalyticsCache()

    assert await cache.connect() is False
    await cache.set("k", 1, ttl=5)
    assert await cache.get("k") == 1  # in-memory fallback still works


# ---- tenant isolation ----------------------------------------------------


def test_cache_keys_are_tenant_scoped() -> None:
    a = module.CacheKey("analytics", "org-a", "dashboard_data", {"x": 1}).generate_key()
    b = module.CacheKey("analytics", "org-b", "dashboard_data", {"x": 1}).generate_key()
    assert a != b
    assert ":org-a:" in a and ":org-b:" in b


async def _seed(cache: module.AnalyticsCache, orgs: List[str]) -> None:
    for org in orgs:
        await cache.set_analytics_data(org, "dashboard_data", {"org": org}, {"f": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("redis_on", [True, False])
async def test_invalidating_one_org_leaves_other_orgs(
    monkeypatch: pytest.MonkeyPatch, redis_on: bool
) -> None:
    client = FakeRedis()
    if redis_on:
        cache, _ = _cache(monkeypatch, client)
        assert await cache.connect() is True
    else:
        monkeypatch.setattr(module, "REDIS_AVAILABLE", False)
        cache = module.AnalyticsCache()
    # "xorg-1" ends with "org-1": a suffix match would wipe it too.
    await _seed(cache, ["org-1", "xorg-1", "org-2"])

    removed = await cache.invalidate_organization_data("org-1")

    assert removed >= 1
    assert await cache.get_analytics_data("org-1", "dashboard_data", {"f": 1}) is None
    for other in ("xorg-1", "org-2"):
        assert await cache.get_analytics_data(other, "dashboard_data", {"f": 1}) == {
            "org": other
        }
    if redis_on:
        assert not any(":org-1:" in k for k in client.data)
        assert any(":xorg-1:" in k for k in client.data)


@pytest.mark.asyncio
async def test_glob_characters_in_an_org_id_match_only_that_org() -> None:
    cache = module.AnalyticsCache()
    cache._redis_enabled = False
    await _seed(cache, ["org-*", "org-1"])

    await cache.invalidate_organization_data("org-*")

    assert await cache.get_analytics_data("org-1", "dashboard_data", {"f": 1}) == {
        "org": "org-1"
    }
    assert await cache.get_analytics_data("org-*", "dashboard_data", {"f": 1}) is None
