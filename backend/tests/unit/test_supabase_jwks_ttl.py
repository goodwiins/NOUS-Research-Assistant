"""R4-M11 + GOO-641 regressions for the Supabase JWKS cache.

R4-M11: the JWKS was cached forever, so a key rotation needed a restart.
Now cached for _SUPABASE_JWKS_TTL_SECONDS; a refetch failure after expiry
must fall back to the stale (but still valid) cache.

GOO-641: the refetch was a blocking ``httpx.get`` on the event loop with no
lock and no negative cache, so a Supabase outage stalled every request for
up to 5 s each. Now an expired cache is served stale while one background
thread refreshes it, the cold fetch is single-flight, failures back off,
and async callers run a cold fetch off the event loop.
"""

import asyncio
import base64
import threading
import time
from typing import Any, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest

import src.core.security as security_module


def _reset() -> None:
    security_module._supabase_jwks_cache = None
    security_module._supabase_jwks_cache_fetched_at = None
    security_module._supabase_jwks_retry_not_before = None
    security_module._supabase_jwks_refresh_thread = None


def _join_refresh() -> None:
    thread = security_module._supabase_jwks_refresh_thread
    if thread is not None:
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _reset_jwks_cache() -> Iterator[None]:
    _reset()
    yield
    _join_refresh()
    _reset()


def _mock_response(payload: Any) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _seed_cache(at: float = 1000.0) -> None:
    with (
        patch("src.core.security.httpx.get") as mock_get,
        patch("src.core.security.time.time", return_value=at),
    ):
        mock_get.return_value = _mock_response({"keys": ["a"]})
        security_module._get_supabase_jwks()


@pytest.mark.unit
def test_first_call_fetches_and_caches() -> None:
    with (
        patch("src.core.security.httpx.get") as mock_get,
        patch("src.core.security.time.time", return_value=1000.0),
    ):
        mock_get.return_value = _mock_response({"keys": ["a"]})
        result = security_module._get_supabase_jwks()

    assert result == {"keys": ["a"]}
    assert mock_get.call_count == 1


@pytest.mark.unit
def test_within_ttl_does_not_refetch() -> None:
    _seed_cache()

    with (
        patch("src.core.security.httpx.get") as mock_get2,
        patch("src.core.security.time.time", return_value=1000.0 + 3599),
    ):
        result = security_module._get_supabase_jwks()

    mock_get2.assert_not_called()
    assert security_module._supabase_jwks_refresh_thread is None
    assert result == {"keys": ["a"]}


@pytest.mark.unit
def test_after_ttl_serves_stale_and_refreshes_in_background() -> None:
    _seed_cache()
    release = threading.Event()

    def _slow_get(*_args: Any, **_kwargs: Any) -> MagicMock:
        release.wait(timeout=5)
        return _mock_response({"keys": ["b"]})

    with (
        patch("src.core.security.httpx.get", side_effect=_slow_get) as mock_get2,
        patch("src.core.security.time.time", return_value=1000.0 + 3601),
    ):
        # The caller gets the stale set immediately; it does not wait on the
        # (still blocked) network fetch.
        assert security_module._get_supabase_jwks() == {"keys": ["a"]}
        # A second expired lookup does not start a second refresh.
        assert security_module._get_supabase_jwks() == {"keys": ["a"]}
        release.set()
        _join_refresh()

    mock_get2.assert_called_once()
    assert security_module._supabase_jwks_cache == {"keys": ["b"]}


@pytest.mark.unit
def test_refetch_failure_after_ttl_returns_stale_cache() -> None:
    _seed_cache()

    with (
        patch("src.core.security.httpx.get", side_effect=Exception("network down")),
        patch("src.core.security.time.time", return_value=1000.0 + 3601),
    ):
        result = security_module._get_supabase_jwks()
        _join_refresh()
        assert security_module._get_supabase_jwks() == {"keys": ["a"]}

    assert result == {"keys": ["a"]}


@pytest.mark.unit
def test_stale_refresh_failure_backs_off() -> None:
    _seed_cache()

    with (
        patch(
            "src.core.security.httpx.get", side_effect=Exception("network down")
        ) as mock_get2,
        patch("src.core.security.time.time", return_value=1000.0 + 3601),
    ):
        for _ in range(5):
            assert security_module._get_supabase_jwks() == {"keys": ["a"]}
            _join_refresh()

    assert mock_get2.call_count == 1


@pytest.mark.unit
def test_cold_fetch_failure_is_negatively_cached() -> None:
    with (
        patch(
            "src.core.security.httpx.get", side_effect=Exception("network down")
        ) as mock_get,
        patch("src.core.security.time.time", return_value=1000.0),
    ):
        for _ in range(3):
            assert security_module._get_supabase_jwks() is None
    assert mock_get.call_count == 1
    with patch("src.core.security.time.time", return_value=1001.0):
        # Async callers keep verification inline during the backoff window.
        assert security_module._supabase_jwks_needs_blocking_fetch() is False

    backoff = security_module._SUPABASE_JWKS_FAILURE_BACKOFF_SECONDS
    with (
        patch("src.core.security.httpx.get") as mock_get2,
        patch("src.core.security.time.time", return_value=1000.0 + backoff + 1),
    ):
        mock_get2.return_value = _mock_response({"keys": ["a"]})
        assert security_module._get_supabase_jwks() == {"keys": ["a"]}
    mock_get2.assert_called_once()


@pytest.mark.unit
def test_concurrent_cold_fetch_is_single_flight() -> None:
    calls = 0
    calls_lock = threading.Lock()

    def _slow_get(*_args: Any, **_kwargs: Any) -> MagicMock:
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.2)
        return _mock_response({"keys": ["a"]})

    results: list[Any] = []

    def _lookup() -> None:
        results.append(security_module._get_supabase_jwks())

    with patch("src.core.security.httpx.get", side_effect=_slow_get):
        threads = [threading.Thread(target=_lookup) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

    assert calls == 1
    assert results == [{"keys": ["a"]}] * 8


def _es256_header_only_token() -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"ES256","typ":"JWT","kid":"k"}')
    return header.decode().rstrip("=") + ".e30.c2ln"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verify_token_async_runs_cold_fetch_off_the_event_loop() -> None:
    """A slow cold JWKS fetch must not stall other coroutines."""

    def _slow_get(*_args: Any, **_kwargs: Any) -> MagicMock:
        time.sleep(0.5)
        return _mock_response({"keys": []})

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    with patch("src.core.security.httpx.get", side_effect=_slow_get) as mock_get:
        ticker = asyncio.create_task(_ticker())
        await asyncio.sleep(0)  # let the ticker start
        result = await security_module.verify_token_async(_es256_header_only_token())
        ticks_when_verify_returned = ticks
        await ticker

    mock_get.assert_called_once()
    assert result is None
    # Had the 0.5 s fetch run on the loop, the ticker (20 x 10 ms) could not
    # have advanced while verify_token_async was awaiting it.
    assert ticks_when_verify_returned >= 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verify_token_async_skips_thread_when_no_fetch_needed() -> None:
    _seed_cache(at=time.time())
    with patch("src.core.security.anyio.to_thread.run_sync") as run_sync:
        await security_module.verify_token_async(_es256_header_only_token())
    run_sync.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verify_token_async_uses_caller_supplied_verifier() -> None:
    seen: list[str] = []

    def _verify(token: str) -> Optional[security_module.TokenData]:
        seen.append(token)
        return None

    assert await security_module.verify_token_async("tok", verify=_verify) is None
    assert seen == ["tok"]
