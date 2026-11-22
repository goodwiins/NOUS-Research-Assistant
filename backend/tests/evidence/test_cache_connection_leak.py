"""GOO-693 review: short-lived event loops must not leak Redis connections.

A per-loop client cache keyed weakly by the loop never dropped its entries
(each client's connections hold their loop), so every ``asyncio.run()`` that
touched the cache left one more Redis connection open.

The test runs a tiny RESP server on a real socket and counts the connections
still open after many short-lived loops.
"""

import asyncio
import gc
import socket
import threading
from typing import Callable, Iterator, List

import pytest

from src.services.evidence import cache as mod

pytestmark = pytest.mark.unit


class _NilRedisServer:
    """Answers PING with PONG and every other command with nil; counts sockets."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self._lock = threading.Lock()
        self._open = 0
        self._stop = False
        self._threads: List[threading.Thread] = []
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def open_connections(self) -> int:
        with self._lock:
            return self._open

    def _accept(self) -> None:
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            with self._lock:
                self._open += 1
            t = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(0.2)
        try:
            while not self._stop:
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                # One reply per command (each starts with '*'): PONG for the
                # client's health-check PING, nil for everything else.
                replies = [
                    b"+PONG\r\n" if b"PING" in cmd.upper() else b"$-1\r\n"
                    for cmd in data.split(b"*")[1:]
                ] or [b"$-1\r\n"]
                conn.sendall(b"".join(replies))
        except OSError:
            pass
        finally:
            conn.close()
            with self._lock:
                self._open -= 1

    def close(self) -> None:
        self._stop = True
        self.sock.close()


@pytest.fixture
def server() -> Iterator[_NilRedisServer]:
    srv = _NilRedisServer()
    yield srv
    srv.close()


def _wait_for(pred: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = threading.Event()
    for _ in range(int(timeout / 0.05)):
        gc.collect()
        if pred():
            return True
        deadline.wait(0.05)
    return bool(pred())


def test_short_lived_loops_do_not_accumulate_connections(
    server: _NilRedisServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod.settings, "REDIS_URL", f"redis://127.0.0.1:{server.port}/0")
    service = mod.EvidenceCacheService()

    async def _one_lookup() -> None:
        assert await service.get_stance_classification("k") is None

    loops = 30
    for _ in range(loops):
        asyncio.run(_one_lookup())

    assert _wait_for(
        lambda: server.open_connections <= 1
    ), f"{server.open_connections} Redis connections still open after {loops} loops"


async def test_a_second_live_loop_gets_a_cache_miss_not_a_new_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: List[object] = []

    class _FakeClient:
        def __init__(self) -> None:
            created.append(self)

    monkeypatch.setattr(mod.redis.Redis, "from_url", lambda *a, **k: _FakeClient())
    service = mod.EvidenceCacheService()

    home = service._client()
    assert home is not None

    other_result: List[object] = []

    def _other_thread() -> None:
        async def _lookup() -> None:
            other_result.append(service._client())

        asyncio.run(_lookup())

    t = threading.Thread(target=_other_thread)
    t.start()
    t.join(5)

    # This loop is still running, so its client stays put; the other loop
    # skips the cache instead of building (and leaking) its own client.
    assert other_result == [None]
    assert service._client() is home
    assert len(created) == 1


def test_a_closed_home_loop_lets_the_next_loop_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: List[object] = []

    def _from_url(*_a: object, **_k: object) -> object:
        created.append(object())
        return created[-1]

    monkeypatch.setattr(mod.redis.Redis, "from_url", _from_url)
    service = mod.EvidenceCacheService()

    async def _lookup() -> object:
        return service._client()

    first = asyncio.run(_lookup())
    second = asyncio.run(_lookup())

    assert first is not None and second is not None
    assert first is not second
    assert len(created) == 2
