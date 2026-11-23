"""GOO-693 review: short-lived event loops must not leak Redis connections.

A per-loop client cache keyed weakly by the loop never dropped its entries
(each client's connections hold their loop), so every ``asyncio.run()`` that
touched the cache left one more Redis connection open.

The test runs a tiny RESP server on a real socket and counts the connections
still open after many short-lived loops.

GOO-698: the old client was only dropped on rebind and its connections
closed whenever the garbage collector got to them, so the leak test passed
only because it called ``gc.collect()``. It now runs with the collector off.
A loop that was stopped but never closed kept its client, and the loop and
client lived in two attributes with no lock.

Mutation checks (each test fails with its guard removed, passes with it):

* ``test_short_lived_loops_do_not_accumulate_connections``: drop the
  ``_hang_up(stale[1])`` call in ``EvidenceCacheService._client``.
* ``test_a_stopped_but_unclosed_loop_lets_the_next_loop_rebind``: test
  ``bound_loop.is_closed()`` again instead of ``is_running()``.
* ``test_a_second_loop_waits_for_the_binding_instead_of_racing_it``: remove
  ``with self._binding_lock:`` (dedent its body).

    pytest -q backend/tests/evidence/test_cache_connection_leak.py
"""

import asyncio
import gc
import socket
import threading
from contextlib import contextmanager
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
    """Poll without collecting garbage: closing must not depend on the GC."""
    pause = threading.Event()
    for _ in range(int(timeout / 0.05)):
        if pred():
            return True
        pause.wait(0.05)
    return bool(pred())


@contextmanager
def _gc_off() -> Iterator[None]:
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


@pytest.mark.parametrize("ending", ["closed", "stopped"])
def test_short_lived_loops_do_not_accumulate_connections(
    server: _NilRedisServer, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    monkeypatch.setattr(mod.settings, "REDIS_URL", f"redis://127.0.0.1:{server.port}/0")
    service = mod.EvidenceCacheService()

    async def _one_lookup() -> None:
        assert await service.get_stance_classification("k") is None

    loops = 50
    stopped: List[asyncio.AbstractEventLoop] = []
    try:
        with _gc_off():
            for _ in range(loops):
                if ending == "closed":
                    asyncio.run(_one_lookup())
                else:  # run, then left stopped but open
                    loop = asyncio.new_event_loop()
                    stopped.append(loop)
                    loop.run_until_complete(_one_lookup())
            assert _wait_for(lambda: server.open_connections <= 1), (
                f"{server.open_connections} Redis connections still open after "
                f"{loops} {ending} loops"
            )
    finally:
        for loop in stopped:
            loop.close()


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


def test_a_stopped_but_unclosed_loop_lets_the_next_loop_rebind(
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

    stopped = asyncio.new_event_loop()
    try:
        first = stopped.run_until_complete(_lookup())
        # The first loop is not running any more (but was never closed): it
        # must not keep the client from every other loop.
        second = asyncio.run(_lookup())
    finally:
        stopped.close()

    assert first is not None
    assert second is not None, "a stopped loop kept the client from every loop"
    assert second is not first


def test_a_second_loop_waits_for_the_binding_instead_of_racing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop/client pair is swapped under a lock.

    While one loop is building its client, another loop must not build and
    bind its own in between: that is how a loop ended up paired with another
    loop's client (or two clients were built and one was lost).
    """
    building, release = threading.Event(), threading.Event()
    created: List[object] = []

    def _slow_from_url(*_a: object, **_k: object) -> object:
        client = object()
        created.append(client)
        if len(created) == 1:
            building.set()
            release.wait(timeout=5)
        return client

    monkeypatch.setattr(mod.redis.Redis, "from_url", _slow_from_url)
    service = mod.EvidenceCacheService()
    results: dict = {}
    second_done = threading.Event()

    def _first() -> None:
        async def _run() -> None:
            results["first"] = service._client()
            # Keep this loop running until the second loop has looked.
            await asyncio.to_thread(second_done.wait, 5)

        asyncio.run(_run())

    def _second() -> None:
        async def _run() -> None:
            results["second"] = service._client()

        try:
            asyncio.run(_run())
        finally:
            second_done.set()

    t1 = threading.Thread(target=_first)
    t1.start()
    assert building.wait(5)
    t2 = threading.Thread(target=_second)
    t2.start()
    t2.join(0.2)  # with the lock it is blocked behind the first binding
    release.set()
    t2.join(5)
    t1.join(5)

    assert results["first"] is created[0]
    # The first loop was still running when the second looked: cache miss.
    assert results["second"] is None, "the second loop bound a client of its own"
    assert len(created) == 1, f"{len(created)} clients built for one binding"
