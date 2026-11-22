"""GOO-643 / RS-1: the evidence cache must not block the event loop.

It used the sync ``redis`` client inside ``async def``, PINGed before every
operation, and connected (5 s timeout) in ``__init__``, which the evidence
router runs at import time.
"""

from typing import Any, Dict, Optional
from unittest.mock import patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from src.services.evidence import cache as mod

pytestmark = pytest.mark.unit


class _FakeAsyncRedis:
    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.pings = 0
        self.fail = False
        self.calls = 0

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self.fail:
            raise RedisConnectionError("down")

    async def ping(self) -> bool:
        self.pings += 1
        self._maybe_fail()
        return True

    async def get(self, key: str) -> Optional[str]:
        self._maybe_fail()
        return self.store.get(key)

    async def setex(self, key: str, _ttl: int, value: str) -> bool:
        self._maybe_fail()
        self.store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        self._maybe_fail()
        return sum(1 for k in keys if self.store.pop(k, None) is not None)

    async def scan_iter(self, match: str, count: int) -> Any:
        self._maybe_fail()
        prefix = match.rstrip("*")
        for key in list(self.store):
            if key.startswith(prefix):
                yield key


def _service(fake: _FakeAsyncRedis) -> mod.EvidenceCacheService:
    service = mod.EvidenceCacheService()
    service.redis_client = fake  # type: ignore[assignment]
    return service


def test_construction_does_no_io() -> None:
    with patch.object(mod.redis.Redis, "from_url") as from_url:
        service = mod.EvidenceCacheService()
    from_url.assert_not_called()
    assert service.redis_client is None


async def test_round_trip_uses_async_client_without_pings() -> None:
    fake = _FakeAsyncRedis()
    service = _service(fake)

    assert await service.set_stance_classification("stance:c:s:m", {"stance": "x"})
    assert await service.get_stance_classification("stance:c:s:m") == {"stance": "x"}
    assert fake.pings == 0


async def test_connection_failure_backs_off() -> None:
    fake = _FakeAsyncRedis()
    fake.fail = True
    service = _service(fake)

    assert await service.get_stance_classification("k") is None
    calls_after_first = fake.calls
    for _ in range(5):
        assert await service.get_stance_classification("k") is None
        assert await service.set_stance_classification("k", {}) is False

    # Inside the backoff window nothing reaches Redis.
    assert fake.calls == calls_after_first == 1


async def test_invalidation_uses_scan_not_keys() -> None:
    fake = _FakeAsyncRedis()
    fake.store = {"stance:abc:s1:m": "{}", "stance:other:s1:m": "{}"}
    service = _service(fake)

    deleted = await service.invalidate_claim_cache("abc")

    assert deleted == 1
    assert list(fake.store) == ["stance:other:s1:m"]


async def test_health_probe_pings_once() -> None:
    fake = _FakeAsyncRedis()
    service = _service(fake)
    assert await service._ensure_connected() is True
    assert fake.pings == 1

