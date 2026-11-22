"""GOO-693: the evidence cache's client and backoff.

The cache is a module-level singleton. It kept one ``redis.asyncio`` client
for the whole process, but that client's connections belong to the event loop
that opened them, so a second loop (Celery's ``run_async``, a test loop) got
"attached to a different loop" errors. Only connection, timeout and OS errors
started the backoff, so a wrong-loop RuntimeError or a protocol error
(``InvalidResponse``) hit Redis again on every request.
"""

import asyncio
from typing import Any, List, Optional
from unittest.mock import patch

import pytest
from redis.exceptions import AuthenticationError, InvalidResponse, ResponseError

from src.services.evidence import cache as mod

pytestmark = pytest.mark.unit


class _FailingRedis:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def get(self, key: str) -> Optional[str]:
        self.calls += 1
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        # New in GOO-693:
        RuntimeError("Task got Future attached to a different loop"),
        InvalidResponse("Protocol error"),
        # Already covered; kept so the broadening cannot drop them:
        AuthenticationError("bad password"),
        asyncio.TimeoutError(),
        OSError("network unreachable"),
    ],
    ids=["wrong-loop", "invalid-response", "auth", "asyncio-timeout", "oserror"],
)
async def test_unusable_redis_backs_off(error: BaseException) -> None:
    fake = _FailingRedis(error)
    service = mod.EvidenceCacheService()
    service.redis_client = fake  # type: ignore[assignment]

    for _ in range(4):
        assert await service.get_stance_classification("k") is None

    assert fake.calls == 1, "every request paid for a broken Redis again"


async def test_a_bad_command_does_not_switch_the_cache_off() -> None:
    fake = _FailingRedis(ResponseError("WRONGTYPE"))
    service = mod.EvidenceCacheService()
    service.redis_client = fake  # type: ignore[assignment]

    for _ in range(3):
        assert await service.get_stance_classification("k") is None

    assert fake.calls == 3


def test_each_event_loop_gets_its_own_client() -> None:
    created: List[Any] = []

    def _from_url(*_args: Any, **_kwargs: Any) -> object:
        client = object()
        created.append(client)
        return client

    service = mod.EvidenceCacheService()

    async def _two_lookups() -> tuple:
        return service._client(), service._client()

    with patch.object(mod.redis.Redis, "from_url", side_effect=_from_url):
        first_a, first_b = asyncio.run(_two_lookups())
        second_a, second_b = asyncio.run(_two_lookups())

    assert first_a is first_b, "one loop should reuse its client"
    assert second_a is second_b
    assert first_a is not second_a, "a second loop reused the first loop's client"
    assert len(created) == 2


def test_an_injected_client_is_used_on_any_loop() -> None:
    service = mod.EvidenceCacheService()
    injected = object()
    service.redis_client = injected  # type: ignore[assignment]

    async def _lookup() -> object:
        return service._client()

    with patch.object(mod.redis.Redis, "from_url") as from_url:
        assert asyncio.run(_lookup()) is injected
        assert asyncio.run(_lookup()) is injected
    from_url.assert_not_called()
