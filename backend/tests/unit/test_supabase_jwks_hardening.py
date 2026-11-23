"""GOO-679: follow-ups to the GOO-641 Supabase JWKS cache.

1. ``verify_token_async`` checked "would a fetch block?" and then verified
   inline; if the failure backoff expired between the two, the inline path
   made the blocking fetch on the event loop.
2. If ``thread.start()`` raised, the refresh marker stayed set and the JWKS
   was never refreshed again.
3. The fetch lock had no timeout and the fetch no overall deadline, so a hung
   fetch could pin every threadpool worker waiting on the lock.

Plus: a 200 with no keys must not replace good keys, and stale keys have a
maximum age.
"""

import base64
import threading
import time
from typing import Any, Iterator, List, Optional
from unittest.mock import MagicMock, patch

import pytest

import src.core.security as security_module

pytestmark = pytest.mark.unit


def _reset() -> None:
    security_module._supabase_jwks_cache = None
    security_module._supabase_jwks_cache_fetched_at = None
    security_module._supabase_jwks_retry_not_before = None
    security_module._supabase_jwks_refresh_thread = None


@pytest.fixture(autouse=True)
def _reset_jwks_cache() -> Iterator[None]:
    _reset()
    yield
    thread = security_module._supabase_jwks_refresh_thread
    if thread is not None:
        thread.join(timeout=5)
    _reset()


def _mock_response(payload: Any) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _es256_header_only_token() -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"ES256","typ":"JWT","kid":"k"}')
    return header.decode().rstrip("=") + ".e30.c2ln"


async def test_inline_path_never_fetches_on_the_loop_when_backoff_expires() -> None:
    """The check says "no fetch needed" (in backoff), then the backoff ends
    before the verifier reads the JWKS: the fetch must still run off-loop."""
    security_module._supabase_jwks_retry_not_before = time.time() + 60
    loop_thread = threading.current_thread()
    # The thread that waits on the network, i.e. the one a fetch would block.
    fetch_threads: List[threading.Thread] = []
    real_fetch = security_module._fetch_supabase_jwks_now

    def _recording_fetch() -> None:
        fetch_threads.append(threading.current_thread())
        real_fetch()

    def _verify(_token: str) -> Optional[security_module.TokenData]:
        # The backoff window closes between the check and the lookup.
        security_module._supabase_jwks_retry_not_before = None
        jwks = security_module._get_supabase_jwks()
        return security_module.TokenData(user_id="u") if jwks else None

    with (
        patch("src.core.security.httpx.get") as mock_get,
        patch.object(security_module, "_fetch_supabase_jwks_now", _recording_fetch),
    ):
        mock_get.return_value = _mock_response({"keys": ["a"]})
        result = await security_module.verify_token_async(
            _es256_header_only_token(), verify=_verify
        )

    assert fetch_threads, "the fetch should still happen, just off the loop"
    assert all(t is not loop_thread for t in fetch_threads)
    assert result is not None and result.user_id == "u"


def test_refresh_marker_is_cleared_when_thread_start_fails() -> None:
    with patch.object(threading.Thread, "start", side_effect=RuntimeError("no")):
        security_module._schedule_jwks_refresh()  # must not raise

    assert security_module._supabase_jwks_refresh_thread is None

    started = threading.Event()
    with patch.object(
        security_module, "_background_jwks_refresh", side_effect=started.set
    ):
        security_module._schedule_jwks_refresh()
        assert started.wait(timeout=2), "a later refresh must still start"


def test_cold_lookup_gives_up_when_the_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security_module, "_SUPABASE_JWKS_LOCK_WAIT_SECONDS", 0.1)
    results: List[Optional[dict]] = []

    with patch("src.core.security.httpx.get") as mock_get:
        with security_module._supabase_jwks_lock:  # a hung fetch holds it
            worker = threading.Thread(
                target=lambda: results.append(security_module._get_supabase_jwks())
            )
            worker.start()
            worker.join(timeout=2)
            assert not worker.is_alive(), "cold lookup waited on the lock forever"

    assert results == [None]
    mock_get.assert_not_called()


def test_hung_fetch_is_cut_off_by_the_overall_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security_module, "_SUPABASE_JWKS_FETCH_DEADLINE_SECONDS", 0.2)
    release = threading.Event()

    def _hung_get(*_args: Any, **_kwargs: Any) -> MagicMock:
        release.wait(timeout=5)  # e.g. DNS that httpx timeouts don't cover
        return _mock_response({"keys": ["late"]})

    try:
        with patch("src.core.security.httpx.get", side_effect=_hung_get):
            started = time.monotonic()
            result = security_module._get_supabase_jwks()
            elapsed = time.monotonic() - started
    finally:
        release.set()

    assert result is None
    assert elapsed < 1.5
    assert security_module._supabase_jwks_retry_not_before is not None
    # The late answer from the abandoned fetch must not land in the cache.
    time.sleep(0.1)
    assert security_module._supabase_jwks_cache is None


@pytest.mark.parametrize("payload", [{"keys": []}, {}, [], {"keys": "x"}])
def test_response_without_keys_does_not_replace_good_keys(payload: Any) -> None:
    with (
        patch("src.core.security.httpx.get") as mock_get,
        patch("src.core.security.time.time", return_value=1000.0),
    ):
        mock_get.return_value = _mock_response({"keys": ["a"]})
        security_module._get_supabase_jwks()

    with patch("src.core.security.httpx.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        with security_module._supabase_jwks_lock:
            security_module._fetch_supabase_jwks_now()

    assert security_module._supabase_jwks_cache == {"keys": ["a"]}
    assert security_module._supabase_jwks_retry_not_before is not None


def test_stale_keys_past_the_maximum_age_are_not_served() -> None:
    with (
        patch("src.core.security.httpx.get") as mock_get,
        patch("src.core.security.time.time", return_value=1000.0),
    ):
        mock_get.return_value = _mock_response({"keys": ["a"]})
        security_module._get_supabase_jwks()

    too_old = 1000.0 + security_module._SUPABASE_JWKS_MAX_STALE_SECONDS + 1
    with (
        patch("src.core.security.httpx.get", side_effect=RuntimeError("down")),
        patch("src.core.security.time.time", return_value=too_old),
    ):
        assert security_module._supabase_jwks_needs_blocking_fetch()
        assert security_module._get_supabase_jwks() is None


def test_stale_keys_inside_the_maximum_age_are_still_served() -> None:
    with (
        patch("src.core.security.httpx.get") as mock_get,
        patch("src.core.security.time.time", return_value=1000.0),
    ):
        mock_get.return_value = _mock_response({"keys": ["a"]})
        security_module._get_supabase_jwks()

    expired = 1000.0 + security_module._SUPABASE_JWKS_TTL_SECONDS + 1
    with (
        patch("src.core.security.httpx.get", side_effect=RuntimeError("down")),
        patch("src.core.security.time.time", return_value=expired),
    ):
        assert security_module._get_supabase_jwks() == {"keys": ["a"]}


async def test_verify_token_async_does_not_hop_threads_with_fresh_keys() -> None:
    security_module._supabase_jwks_cache = {"keys": ["a"]}
    security_module._supabase_jwks_cache_fetched_at = time.time()
    with patch("src.core.security.anyio.to_thread.run_sync") as run_sync:
        await security_module.verify_token_async(_es256_header_only_token())
    run_sync.assert_not_called()


def test_module_imports_anyio_to_thread_explicitly() -> None:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(security_module))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "anyio.to_thread" in imported
