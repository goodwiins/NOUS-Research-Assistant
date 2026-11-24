"""The analytics cache stays consistent across a Redis outage (GOO-736).

Follow-ups to the GOO-654 circuit breaker:

* A delete (or write) made while Redis is unreachable used to reach only the
  pod's memory, so after the reconnect Redis served the old value until its
  TTL. The cache now remembers those keys and patterns (bounded) and replays
  the deletes before it serves from Redis again.
* While Redis is healthy, a Redis miss is a miss: the pod's own local copy
  may be older than what another pod deleted or let expire.
* Every key lives under ``analytics:``, so one SCAN covers the whole cache
  and an overflowing replay can flush exactly that namespace.
* Organization ids in keys must be UUIDs: a ``:`` in an id would shift the
  key segments that patterns rely on.

Mutation check for the replay (docs/engineering/testing.md): disable the
``await self._replay_invalidations(client)`` call in
``AnalyticsCache._init_redis`` (backend/src/cache/analytics_cache.py) and
``pytest -q backend/tests/unit/services/test_analytics_cache_outage_consistency.py
-k replay`` fails with the deleted key still served from Redis.
"""

from __future__ import annotations

import asyncio
import fnmatch
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.cache import analytics_cache as module
from src.services.quality import performance_dashboard_service as dashboard
from src.services.quality.performance_dashboard_service import MetricTimeRange

pytestmark = pytest.mark.unit

ORG = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))


def _key(name: str) -> str:
    return module.analytics_key("test", name, ORG)


class FakeRedis:
    """A shared Redis that can be taken down and brought back."""

    def __init__(self) -> None:
        self.data: Dict[str, str] = {}
        self.ping = AsyncMock()
        self.down = False
        self.delete_gate: Optional[asyncio.Event] = None

    def _check(self) -> None:
        if self.down:
            raise ConnectionError("redis down")

    def go_down(self) -> None:
        self.down = True
        self.ping.side_effect = ConnectionError("redis down")

    def come_back(self) -> None:
        self.down = False
        self.ping.side_effect = None

    async def get(self, key: str) -> Optional[str]:
        self._check()
        return self.data.get(key)

    async def setex(self, key: str, _ttl: int, value: str) -> None:
        self._check()
        self.data[key] = value

    async def delete(self, *keys: str) -> int:
        self._check()
        if self.delete_gate is not None:
            await self.delete_gate.wait()
        return sum(1 for k in keys if self.data.pop(k, None) is not None)

    async def scan_iter(self, match: str, count: int = 0) -> AsyncIterator[str]:
        self._check()
        for key in list(self.data):
            if fnmatch.fnmatchcase(key, match.replace("\\", "")):
                yield key

    async def aclose(self) -> None:
        return None


def _cache(
    monkeypatch: pytest.MonkeyPatch,
) -> Tuple[module.AnalyticsCache, FakeRedis]:
    client = FakeRedis()
    monkeypatch.setattr(module, "REDIS_AVAILABLE", True)
    monkeypatch.setattr(
        module,
        "get_analytics_config",
        lambda: MagicMock(redis_analytics_url="redis://analytics:6379/0"),
    )
    monkeypatch.setattr(module.redis, "from_url", MagicMock(return_value=client))
    return module.AnalyticsCache(), client


async def _outage(cache: module.AnalyticsCache, client: FakeRedis) -> None:
    """Take Redis down and let the cache notice (breaker opens)."""
    client.go_down()
    await cache.get(_key("probe"))
    assert cache._connected is False


async def _reconnect(cache: module.AnalyticsCache, client: FakeRedis) -> bool:
    client.come_back()
    cache._next_connect_attempt = 0.0  # the breaker window elapses
    return await cache.connect()


# ---- item 1: deletes made during an outage are replayed --------------------


@pytest.mark.asyncio
async def test_replay_delete_made_while_breaker_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, client = _cache(monkeypatch)
    assert await cache.connect() is True
    await cache.set(_key("k"), {"v": "old"}, ttl=180)
    await _outage(cache, client)

    await cache.delete(_key("k"))

    assert await _reconnect(cache, client) is True
    assert _key("k") not in client.data
    assert await cache.get(_key("k")) is None


@pytest.mark.asyncio
async def test_replay_delete_whose_redis_call_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, client = _cache(monkeypatch)
    assert await cache.connect() is True
    await cache.set(_key("k"), 1, ttl=180)
    client.down = True  # the delete itself is the call that fails

    assert await cache.delete(_key("k")) is False
    assert cache._connected is False

    assert await _reconnect(cache, client) is True
    assert _key("k") not in client.data


@pytest.mark.asyncio
async def test_replay_delete_made_before_the_first_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, client = _cache(monkeypatch)
    client.data[_key("k")] = '"written by another pod"'
    client.go_down()

    await cache.delete(_key("k"))  # cold: memory only, connect attempt fails
    for _ in range(5):
        await asyncio.sleep(0)

    assert await _reconnect(cache, client) is True
    assert _key("k") not in client.data


@pytest.mark.asyncio
async def test_replay_pattern_invalidated_during_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, client = _cache(monkeypatch)
    assert await cache.connect() is True
    await cache.set(_key("a"), 1, ttl=180)
    await cache.set(module.analytics_key("other", "b", ORG), 2, ttl=180)
    await _outage(cache, client)

    await cache.invalidate_pattern("analytics:test:*")

    assert await _reconnect(cache, client) is True
    assert _key("a") not in client.data
    assert module.analytics_key("other", "b", ORG) in client.data


@pytest.mark.asyncio
async def test_replay_write_made_during_outage_drops_the_older_redis_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, client = _cache(monkeypatch)
    assert await cache.connect() is True
    await cache.set(_key("k"), "old", ttl=180)
    await _outage(cache, client)

    await cache.set(_key("k"), "new", ttl=180)  # memory only

    assert await _reconnect(cache, client) is True
    assert await cache.get(_key("k")) != "old"


@pytest.mark.asyncio
async def test_replay_overflow_flushes_only_the_analytics_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_MAX_PENDING_INVALIDATIONS", 3)
    cache, client = _cache(monkeypatch)
    assert await cache.connect() is True
    await cache.set(_key("kept-by-nobody"), 1, ttl=180)
    client.data["celery-task-meta-1"] = "{}"  # the Redis is shared
    await _outage(cache, client)

    for i in range(10):
        await cache.delete(_key(f"d{i}"))

    # Bounded: the queue collapsed into one namespace flush.
    assert len(cache._pending_keys) + len(cache._pending_patterns) <= 3
    assert cache._pending_flush is True

    assert await _reconnect(cache, client) is True
    assert not any(k.startswith("analytics:") for k in client.data)
    assert "celery-task-meta-1" in client.data


@pytest.mark.asyncio
async def test_replay_failure_keeps_the_queue_and_stays_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, client = _cache(monkeypatch)
    assert await cache.connect() is True
    await cache.set(_key("k"), "old", ttl=180)
    await _outage(cache, client)
    await cache.delete(_key("k"))

    # Ping answers but the replayed delete fails: do not serve from Redis.
    client.ping.side_effect = None
    cache._next_connect_attempt = 0.0
    assert await cache.connect() is False
    assert _key("k") in cache._pending_keys

    assert await _reconnect(cache, client) is True
    assert _key("k") not in client.data


@pytest.mark.asyncio
async def test_replay_includes_deletes_that_arrive_while_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, client = _cache(monkeypatch)
    assert await cache.connect() is True
    await cache.set(_key("a"), 1, ttl=180)
    await cache.set(_key("b"), 2, ttl=180)
    await _outage(cache, client)
    await cache.delete(_key("a"))

    client.come_back()
    client.delete_gate = asyncio.Event()
    cache._next_connect_attempt = 0.0
    connecting = asyncio.ensure_future(cache.connect())
    for _ in range(5):
        await asyncio.sleep(0)
    assert cache._connected is False  # replay in flight
    await cache.delete(_key("b"))  # still memory-only: queued
    client.delete_gate.set()

    assert await connecting is True
    assert _key("a") not in client.data
    assert _key("b") not in client.data
    assert not cache._pending_keys


@pytest.mark.asyncio
async def test_nothing_is_queued_when_redis_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "REDIS_AVAILABLE", False)
    cache = module.AnalyticsCache()

    await cache.set(_key("k"), 1, ttl=60)
    await cache.delete(_key("k"))

    assert not cache._pending_keys and not cache._pending_flush


# ---- item 2: a Redis miss is a miss while Redis is healthy -----------------


@pytest.mark.asyncio
async def test_redis_miss_does_not_fall_back_to_the_local_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, client = _cache(monkeypatch)
    assert await cache.connect() is True
    await cache.set(_key("k"), "old", ttl=180)

    del client.data[_key("k")]  # another pod deleted it, or it expired

    assert await cache.get(_key("k")) is None


@pytest.mark.asyncio
async def test_local_copy_still_serves_during_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, client = _cache(monkeypatch)
    assert await cache.connect() is True
    await cache.set(_key("k"), "v", ttl=180)
    client.go_down()

    assert await cache.get(_key("k")) == "v"


# ---- item 4: one namespace ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["get", "set", "delete", "invalidate_pattern"])
async def test_keys_outside_the_namespace_are_rejected(op: str) -> None:
    cache = module.AnalyticsCache()
    call: Any = getattr(cache, op)
    args: List[Any] = ["perf_dashboard:overview:x"]
    if op == "set":
        args.append(1)
    with pytest.raises(ValueError):
        await call(*args)


def test_dashboard_overview_key_is_in_the_namespace() -> None:
    key = dashboard._overview_cache_key(ORG, MetricTimeRange.LAST_24H)
    assert key == f"analytics:perf_dashboard:overview:{ORG}:24h"


def test_dashboard_overview_is_not_cached_for_a_caller_without_an_org() -> None:
    # str(None) used to give every org-less caller one shared entry.
    assert dashboard._overview_cache_key("None", MetricTimeRange.LAST_24H) is None


@pytest.mark.asyncio
async def test_health_check_probe_key_is_in_the_namespace() -> None:
    cache = module.AnalyticsCache()
    cache._redis_enabled = False
    seen: List[str] = []
    real_set = cache.set

    async def spy(
        key: str, value: Any, ttl: int = 300, use_fallback: bool = True
    ) -> bool:
        seen.append(key)
        return await real_set(key, value, ttl, use_fallback)

    cache.set = spy  # type: ignore[method-assign]
    health = await cache.health_check()

    assert health["fallback_status"] == "healthy"
    assert seen and all(k.startswith("analytics:") for k in seen)


# ---- item 5: organization ids are UUIDs ------------------------------------


@pytest.mark.parametrize("bad", ["org:1", "org-1", "None", "", "*"])
def test_non_uuid_org_ids_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        module.CacheKey("analytics", bad, "dashboard_data", {}).generate_key()
    with pytest.raises(ValueError):
        module.analytics_key("perf_dashboard", "overview", bad)


def test_org_ids_are_canonicalised() -> None:
    as_uuid = uuid.UUID(ORG)
    assert module.analytics_key("s", "k", as_uuid) == module.analytics_key(
        "s", "k", ORG.upper()
    )


def test_other_key_segments_cannot_contain_a_separator() -> None:
    with pytest.raises(ValueError):
        module.analytics_key("perf:dashboard", "overview", ORG)
    with pytest.raises(ValueError):
        module.analytics_key("perf_dashboard", "overview", ORG, "24:h")
